"""Offline RUPunct-small INT8 evaluation; package inference is shared with Voqelis.

Run from the worktree root using:
PYTHONPATH=src python -m tools.rupunct_probe --model-dir /path/to/model
"""

from __future__ import annotations

import argparse
import resource
import statistics
import sys
import time
from pathlib import Path

from voqelis.rupunct import (
    OnnxRUPunct,
    plan_token_windows,
    render_labeled_words,
)

CASES = (
    "проверка распознавания речи сегодня хорошая погода а завтра продолжим тестирование",
    "сегодня я закончил работу завтра в девять часов начну новую задачу",
    "что мне запланировать на завтра вечером",
    "завтра в 21:00 ужинать потом в 22:00 читать книгу",
    "послезавтра в 13 часов позвонить клиенту и закончить отчёт",
    "сегодня я купил молоко хлеб и яблоки",
    "я хотел выйти на прогулку но начался дождь",
    "сколько времени займёт поездка и когда мы вернёмся",
    "завтра в 13:00 созвон потом в 14:30 встреча",
    "завтра с 23:00 до 01:00 читать книгу",
    "в 20:00 проверить почту",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--text", action="append", default=[], help="Optional custom text")
    args = parser.parse_args()
    started = time.perf_counter()
    model = OnnxRUPunct(args.model_dir)
    print(f"Model load time: {time.perf_counter() - started:.3f}s")
    print(
        "Process peak RSS after load: "
        f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB"
    )
    timings = []
    for text in (*CASES, *args.text):
        t0 = time.perf_counter()
        try:
            output = model.punctuate(text)
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        timings.append((time.perf_counter() - t0) * 1000)
        print(f"INPUT:  {text}\nOUTPUT: {output}\nLATENCY_MS: {timings[-1]:.1f}\n")
    print(f"Median latency: {statistics.median(timings):.1f} ms")
    print(f"Max latency: {max(timings):.1f} ms")
    print(
        "Process peak RSS: "
        f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB"
    )
    print("Note: manual quality review required; this benchmark does not prove accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
