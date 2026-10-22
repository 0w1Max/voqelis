"""Evaluate text-only punctuation against held-out Russian FLEURS references.

Uses existing local JSON manifests and model files. Does not download data,
decode audio, modify production settings or alter the source ASR transcript.
FLEURS gold-word input isolates punctuation quality from ASR word errors.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import resource
import statistics
import time
from pathlib import Path
from typing import Any

from voqelis.rupunct import OnnxRUPunct

WORD = re.compile(r"[^\W_]+", re.UNICODE)
MARK = re.compile(r"[,.;:!?…—–-]+")


def tokens(text: str) -> list[str]:
    return [
        item.group().casefold().replace("ё", "е")
        for item in WORD.finditer(text)
    ]


def boundaries(text: str) -> tuple[list[str], list[str], list[bool]]:
    """Punctuation after each lexical token and casing at its beginning."""
    matches = list(WORD.finditer(text))
    words = tokens(text)
    punctuation = []
    uppercase = []
    for index, match in enumerate(matches):
        gap_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )
        gap = text[match.end():gap_end]
        punctuation.append("".join(MARK.findall(gap)))
        uppercase.append(match.group()[0].isupper())
    return words, punctuation, uppercase


def choose_unique(
    rows: list[dict[str, Any]],
    max_duration: float = 25.0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Deduplicate prompt text to avoid overweighting repeated recordings."""
    chosen = []
    seen: set[tuple[str, ...]] = set()
    stats = {"total": 0, "over_duration": 0, "duplicate_text": 0}
    for row in rows:
        stats["total"] += 1
        text = row.get("reference_words")
        gold = row.get("reference_punctuation")
        duration = row.get("duration_seconds")
        if not isinstance(text, str) or not isinstance(gold, str):
            raise ValueError("manifest is missing text references")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration):
            raise ValueError("manifest contains invalid duration")
        if duration <= 0:
            raise ValueError("manifest contains nonpositive duration")
        if duration > max_duration:
            stats["over_duration"] += 1
            continue
        identity = tuple(tokens(text))
        if not identity:
            raise ValueError("manifest contains an empty transcript")
        if identity in seen:
            stats["duplicate_text"] += 1
            continue
        seen.add(identity)
        chosen.append(row)
    return chosen, stats


def boundary_counts(gold: list[str], predicted: list[str]) -> dict[str, int]:
    if len(gold) != len(predicted):
        raise ValueError("boundary count mismatch")
    tp = fp = fn = correct = 0
    for wanted, actual in zip(gold, predicted, strict=True):
        correct += wanted == actual
        if wanted == actual and wanted:
            tp += 1
        elif wanted != actual:
            fp += bool(actual)
            fn += bool(wanted)
    return {"correct": correct, "total": len(gold), "tp": tp, "fp": fp, "fn": fn}


def punctuation_summary(counts: dict[str, int]) -> dict[str, float | int]:
    denominator = 2 * counts["tp"] + counts["fp"] + counts["fn"]
    return {
        **counts,
        "exact_boundary_accuracy_pct": round(
            100 * counts["correct"] / counts["total"], 2
        ) if counts["total"] else 0.0,
        "exact_mark_f1_pct": round(
            100 * 2 * counts["tp"] / denominator, 2
        ) if denominator else 100.0,
    }


def evaluate(
    rows: list[dict[str, Any]],
    punctuate: Any,
    *,
    max_duration: float = 25.0,
) -> dict[str, Any]:
    chosen, selection = choose_unique(rows, max_duration)
    totals = {
        label: {"correct": 0, "total": 0, "tp": 0, "fp": 0, "fn": 0}
        for label in ("raw", "restored")
    }
    case_correct = case_total = eligible = unchanged = 0
    samples = []
    latencies = []
    for row in chosen:
        raw = row["reference_words"]
        gold = row["reference_punctuation"]
        start = time.perf_counter()
        restored = punctuate(raw)
        latency_ms = (time.perf_counter() - start) * 1000
        if not isinstance(restored, str):
            raise TypeError("punctuator returned non-text data")
        # Stronger than simply comparing word counts: the normalized exact
        # sequence must survive every punctuation operation.
        if tokens(restored) != tokens(raw):
            raise ValueError("punctuator changed recognized lexical tokens")
        latencies.append(latency_ms)
        unchanged += restored == raw
        gold_words, gold_marks, gold_cases = boundaries(gold)
        original_words, original_marks, _ = boundaries(raw)
        predicted_words, predicted_marks, predicted_cases = boundaries(restored)
        is_eligible = (
            gold_words == original_words == predicted_words
            and len(gold_marks) == len(predicted_marks)
        )
        record = {
            "id": row.get("id"),
            "file": row.get("file"),
            "reference": gold,
            "input": raw,
            "output": restored,
            "latency_ms": round(latency_ms, 3),
            "eligible": is_eligible,
        }
        if is_eligible:
            eligible += 1
            for label, marks in (("raw", original_marks), ("restored", predicted_marks)):
                counts = boundary_counts(gold_marks, marks)
                for key in totals[label]:
                    totals[label][key] += counts[key]
            correct = sum(
                a == b for a, b in zip(gold_cases, predicted_cases, strict=True)
            )
            case_correct += correct
            case_total += len(gold_cases)
            record["restored_wrong_boundaries"] = [
                {
                    "after": gold_words[i],
                    "expected": gold_marks[i],
                    "actual": predicted_marks[i],
                }
                for i in range(len(gold_marks))
                if gold_marks[i] != predicted_marks[i]
            ]
        else:
            record["skip_reason"] = "reference words differ from input words"
        samples.append(record)

    ordered = sorted(latencies)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else 0.0
    return {
        "selection": {
            **selection,
            "unique_selected": len(chosen),
            "lexically_comparable": eligible,
            "not_lexically_comparable": len(chosen) - eligible,
            "unchanged_by_punctuator": unchanged,
        },
        "punctuation_raw": punctuation_summary(totals["raw"]),
        "punctuation_restored": punctuation_summary(totals["restored"]),
        "capitalization_initial_accuracy_pct": round(
            100 * case_correct / case_total, 2
        ) if case_total else None,
        "latency_ms": {
            "p50": round(statistics.median(ordered), 3) if ordered else 0.0,
            "p95": round(p95, 3),
            "max": round(max(ordered), 3) if ordered else 0.0,
        },
        "process_peak_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
        ),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists():
        parser.error("output already exists; refusing to overwrite")
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        parser.error("manifest must contain a JSON list")
    model = OnnxRUPunct(args.model_dir)
    result = evaluate(data, model.punctuate)
    # Do not count a tiny or entirely incomparable sample as a pass.
    if result["selection"]["lexically_comparable"] < 10:
        raise SystemExit("STOP: fewer than ten lexically comparable references")

    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    display = {k: v for k, v in result.items() if k != "samples"}
    print(json.dumps(display, ensure_ascii=False, indent=2))
    print("\nFive examples of punctuation errors:")
    shown = 0
    for sample in result["samples"]:
        errors = sample.get("restored_wrong_boundaries", [])
        if errors:
            print(
                f'{sample["file"]}: {len(errors)} errors | '
                f'{sample["output"][:140]}'
            )
            shown += 1
            if shown == 5:
                break
    print(f"\nSaved full report: {args.output}")
    print("FLEURS gold-word evaluation; verify actual RNNT transcripts separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
