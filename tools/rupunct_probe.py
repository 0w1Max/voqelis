"""Offline RUPunct_small INT8 evaluation. No network access or production hooks.

Optional runtime: numpy, onnxruntime, tokenizers (not production dependencies).
Only uses model files supplied via --model-dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import resource
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

MODEL_SHA256 = "b105da023474d98aa13ba18953ae67b04b17bd0595034bc06030c17536893933"
MAX_SUBTOKENS = 256
MAX_WORDS = 2048
MAX_WINDOWS = 64
OVERLAP_WORDS = 12
_TIME_TOKEN = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\Z")
_SAFE_FINAL_TIME_MARKS = frozenset({
    "PERIOD", "QUESTION", "VOSKL", "QUESTIONVOSKL", "MNOGOTOCHIE",
})
CASE_MODES = {"LOWER", "UPPER", "UPPER_TOTAL"}
MARKS = {
    "O": "", "PERIOD": ".", "COMMA": ",", "QUESTION": "?",
    "TIRE": " —", "VOSKL": "!", "DVOETOCHIE": ":",
    "PERIODCOMMA": ";", "DEFIS": "-", "QUESTIONVOSKL": "?!",
    "MNOGOTOCHIE": "...",
}
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


def render_labeled_words(text: str, labels: list[str]) -> str:
    """Only change casing and append punctuation, preserving every lexical word."""
    words = text.split()
    if len(words) != len(labels):
        raise ValueError("word/label count differs")
    if not words:
        return text
    # Do not reinterpret an already punctuated transcript.
    if any(re.search(r"[.,!?;:…]$", word) for word in words):
        return text
    out = []
    for position, (word, label) in enumerate(zip(words, labels, strict=True)):
        case, sep, mark = label.rpartition("_")
        if not sep or case not in CASE_MODES or mark not in MARKS:
            raise ValueError(f"unsupported model label: {label!r}")
        if case == "UPPER_TOTAL":
            adjusted = word.upper()
        elif case == "UPPER":
            adjusted = word[:1].upper() + word[1:]
        else:
            adjusted = word
        if adjusted.casefold() != word.casefold():
            raise ValueError("model attempted to change lexical content")
        # ASR time expressions are structured facts, not punctuation anchors.
        # In particular, "21:00:" must never be produced from "21:00".
        # Retain sentence-final punctuation, but never punctuate a time
        # expression followed by other recognized words.
        if _TIME_TOKEN.fullmatch(word) and (
            position < len(words) - 1 or mark not in _SAFE_FINAL_TIME_MARKS
        ):
            mark = "O"
        out.append(adjusted + MARKS[mark])
    return " ".join(out)


def plan_token_windows(
    word_count: int,
    token_count: Callable[[int, int], int],
    *,
    max_subtokens: int = MAX_SUBTOKENS,
    overlap_words: int = OVERLAP_WORDS,
    max_windows: int = MAX_WINDOWS,
) -> list[tuple[int, int, int]]:
    """Plan bounded windows (start, end, emit_end) without dropping any words.

    All indices refer to original words. Windows can overlap to avoid predicting
    end-of-sentence punctuation merely because an inference window ended.
    Model labels from the overlapping tail are discarded and recomputed in the
    next window. If no safe bounded plan exists, fail rather than truncate.
    """
    if word_count < 0 or max_subtokens < 1 or overlap_words < 0 or max_windows < 1:
        raise ValueError("invalid window planning parameters")
    windows: list[tuple[int, int, int]] = []
    start = 0
    while start < word_count:
        if len(windows) >= max_windows:
            raise ValueError("too many model windows")
        low, high, best = start + 1, word_count, start
        while low <= high:
            middle = (low + high) // 2
            if token_count(start, middle) <= max_subtokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == start:
            raise ValueError("single word exceeds model token budget")
        width = best - start
        overlap = min(overlap_words, width // 4) if best < word_count else 0
        emit_end = best - overlap
        if emit_end <= start:
            raise ValueError("model window made no progress")
        windows.append((start, best, emit_end))
        start = emit_end
    return windows


class OnnxRUPunct:
    def __init__(self, model_dir: Path) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Benchmark-only dependencies missing: numpy, onnxruntime, tokenizers"
            ) from exc

        root = model_dir.resolve(strict=True)
        paths = [root / name for name in (
            "rupunct_small_int8.onnx", "tokenizer.json", "config.json"
        )]
        if not all(p.is_file() and not p.is_symlink() for p in paths):
            raise ValueError("Model directory requires ONNX, tokenizer.json, config.json")
        digest = hashlib.sha256(paths[0].read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            raise ValueError(f"ONNX SHA256 mismatch: {digest}")
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(paths[1]))
        config = json.loads(paths[2].read_text(encoding="utf-8"))
        self.labels = {int(k): v for k, v in config["id2label"].items()}
        if len(self.labels) != 33:
            raise ValueError("Expected RUPunct's 33-label classification head")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(
            str(paths[0]), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        if {x.name for x in self.session.get_inputs()} != {
            "input_ids", "attention_mask", "token_type_ids"
        }:
            raise ValueError("Unexpected ONNX inputs")

    def _predict_labels(self, words: list[str]) -> list[str]:
        encoding = self.tokenizer.encode(words, is_pretokenized=True)
        if len(encoding.ids) > MAX_SUBTOKENS:
            raise ValueError("token budget exceeded during inference")
        first_positions: dict[int, int] = {}
        for index, word_id in enumerate(encoding.word_ids):
            if word_id is not None and word_id not in first_positions:
                first_positions[word_id] = index
        if sorted(first_positions) != list(range(len(words))):
            raise ValueError("Tokenizer lost word alignment")
        np = self.np
        inputs = {
            "input_ids": np.asarray([encoding.ids], dtype=np.int64),
            "attention_mask": np.asarray([encoding.attention_mask], dtype=np.int64),
            "token_type_ids": np.asarray([encoding.type_ids], dtype=np.int64),
        }
        logits = self.session.run(None, inputs)[0]
        if logits.shape != (1, len(encoding.ids), 33):
            raise ValueError(f"Unexpected ONNX output shape: {logits.shape}")
        predictions = np.argmax(logits[0], axis=-1)
        return [self.labels[int(predictions[first_positions[i]])] for i in range(len(words))]

    def punctuate(self, text: str) -> str:
        words = text.split()
        if not words:
            return text
        if any(re.search(r"[.,!?;:…]$", word) for word in words):
            return text
        if len(words) > MAX_WORDS:
            return text  # never block the application on unusually long input

        def count_tokens(start: int, end: int) -> int:
            return len(
                self.tokenizer.encode(words[start:end], is_pretokenized=True).ids
            )

        try:
            windows = plan_token_windows(len(words), count_tokens)
        except ValueError:
            return text  # impossible window: preserve original, never truncate
        all_labels: list[str] = []
        for start, end, emit_end in windows:
            labels = self._predict_labels(words[start:end])
            all_labels.extend(labels[:emit_end - start])
        if len(all_labels) != len(words):
            raise ValueError("Chunking lost or duplicated input words")
        return render_labeled_words(text, all_labels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--text", action="append", default=[], help="Optional custom text")
    args = parser.parse_args()
    started = time.perf_counter()
    model = OnnxRUPunct(args.model_dir)
    print(f"Model load time: {time.perf_counter() - started:.3f}s")
    # Linux ru_maxrss is KiB; systemd-run --pipe's reported peak can be
    # misleading for a transient unit. Measure the Python process itself.
    print(f"Process peak RSS after load: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB")
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
    print(f"Process peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB")
    print("Note: manual quality review required; this benchmark does not prove accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
