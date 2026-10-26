"""Isolated, ONNX-only, word-preserving Horizon punctuation benchmark.

No downloads or production integration. Accepts short, unpunctuated ASR texts.
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import statistics
import time
from pathlib import Path

MODEL_NAME = "Horizon-Labs/punctuation-restoration-small"
MODEL_REVISION = "ee31fdf"  # upstream v1.1, 2026-10-09
MODEL_FILES = ("model_quantized.onnx", "tokenizer.json", "config.json")
ALLOWED_MARKS = frozenset({"0", ".", ",", "?", ":", "-"})
MAX_TOKENS = 256
MAX_WORDS = 110
LEXICAL = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)

CASES = (
    "друг общался раздраженно был в своих мыслях мы разошлись по домам",
    "друг общался раздраженно был в своих мыслях",
    "сегодня я закончил работу завтра в девять часов начну новую задачу",
    "завтра в 13:00 созвон потом в 14:30 встреча",
    "завтра с 23:00 до 01:00 читать книгу",
    "сегодня я купил молоко хлеб и яблоки",
    "я хотел выйти на прогулку но начался дождь",
    "сколько времени займёт поездка и когда мы вернёмся",
    "друг сказал что устал но мы все равно пошли гулять",
    "сегодня я записываю факты дня завтра хочу составить план",
    "я волновался когда получил сообщение потом немного успокоился",
    "я не знаю почему он молчал мы поговорили и разошлись",
)


def last_token_indices(word_count: int, token_word_ids: list[int | None]) -> list[int]:
    """Find the last subtoken of every pretokenized word, without offset guessing.

    mmBERT normalization can produce character offsets that do not end exactly
    where the original Unicode word ends. Rust tokenizers provides stable
    word_ids when invoked with is_pretokenized=True; use those instead.
    Missing, reordered or truncated words fail closed.
    """
    if word_count < 1:
        raise ValueError("No input words")
    positions = [-1] * word_count
    previous = -1
    for position, word_id in enumerate(token_word_ids):
        if word_id is None:
            continue  # special tokens
        if (
            isinstance(word_id, bool)
            or not isinstance(word_id, int)
            or not 0 <= word_id < word_count
            or word_id < previous
        ):
            raise ValueError("Tokenizer returned invalid or reordered word IDs")
        positions[word_id] = position
        previous = word_id
    if any(position < 0 for position in positions):
        raise ValueError("Tokenizer lost or truncated input words")
    return positions

def render_marks(text: str, marks: list[str]) -> str:
    """Append punctuation without ever replacing spoken words."""
    words = text.split()
    if len(words) != len(marks):
        raise ValueError("Word/mark count mismatch")
    if any(w.endswith((".", ",", "?", "!", ";", ":")) for w in words):
        raise ValueError("Input already contains terminal punctuation")
    output = []
    for word, mark in zip(words, marks, strict=True):
        if mark not in ALLOWED_MARKS:
            raise ValueError(f"Unexpected label: {mark!r}")
        if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", word) and mark != "0":
            mark = "0"
        if mark == "-":
            mark = "0"  # do not manufacture hyphenated words
        output.append(word + ("" if mark == "0" else mark))
    result = " ".join(output)
    if [w.casefold() for w in LEXICAL.findall(result)] != [
        w.casefold() for w in LEXICAL.findall(text)
    ]:
        raise ValueError("Lexical content was changed")
    return result


def read_model_info(model_dir: Path) -> dict[int, str]:
    required = [model_dir / name for name in MODEL_FILES]
    if not all(p.is_file() and not p.is_symlink() for p in required):
        raise ValueError("Missing or unsafe ONNX/tokenizer/config file")
    if required[0].stat().st_size < 200_000_000:
        raise ValueError("ONNX is incomplete or an LFS pointer")
    if required[1].stat().st_size < 1_000_000:
        raise ValueError("Tokenizer is incomplete")
    config = json.loads(required[2].read_text(encoding="utf-8"))
    labels = {int(i): str(value) for i, value in config["id2label"].items()}
    if set(labels.values()) != ALLOWED_MARKS or len(labels) != len(ALLOWED_MARKS):
        raise ValueError(f"Unexpected punctuation labels: {labels}")
    return labels


class HorizonONNX:
    def __init__(self, model_dir: Path) -> None:
        # Lazy imports keep pure regression tests light.
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.labels = read_model_info(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(
            str(model_dir / "model_quantized.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        model_inputs = {item.name for item in self.session.get_inputs()}
        if not {"input_ids", "attention_mask"} <= model_inputs:
            raise ValueError(f"Required ONNX inputs missing: {model_inputs}")
        allowed_inputs = {"input_ids", "attention_mask", "token_type_ids", "position_ids"}
        if model_inputs - allowed_inputs:
            raise ValueError(f"Unsupported ONNX inputs: {model_inputs}")
        self.model_inputs = self.session.get_inputs()

    def punctuate(self, text: str) -> str:
        if not text.strip() or len(text.split()) > MAX_WORDS:
            raise ValueError("Empty/oversize input")
        # Pretokenized input assigns an explicit word ID to every subtoken.
        # Raw character offsets are not reliable around normalized Cyrillic.
        words = text.split()
        encoding = self.tokenizer.encode(words, is_pretokenized=True)
        if len(encoding.ids) > MAX_TOKENS:
            raise ValueError("Token budget exceeded; refusing truncation")
        locations = last_token_indices(len(words), encoding.word_ids)
        values = {
            "input_ids": encoding.ids,
            "attention_mask": encoding.attention_mask,
            "token_type_ids": encoding.type_ids,
            "position_ids": list(range(len(encoding.ids))),
        }
        feed = {}
        for item in self.model_inputs:
            if item.type not in {"tensor(int32)", "tensor(int64)"}:
                raise ValueError(f"Unexpected ONNX dtype: {item.type}")
            dtype = self.np.int32 if item.type == "tensor(int32)" else self.np.int64
            feed[item.name] = self.np.asarray([values[item.name]], dtype=dtype)
        logits = self.session.run(None, feed)[0]
        if logits.shape != (1, len(encoding.ids), len(self.labels)):
            raise ValueError(f"Unexpected ONNX logits: {logits.shape}")
        labels = self.np.argmax(logits[0], axis=-1)
        marks = [self.labels[int(labels[pos])] for pos in locations]
        return render_marks(text, marks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--text", action="append", default=[])
    args = parser.parse_args()
    t0 = time.monotonic()
    model = HorizonONNX(args.model_dir)
    print(f"HORIZON MODEL LOAD: {time.monotonic() - t0:.3f}s")
    timings = []
    for raw in [*CASES, *args.text]:
        started = time.monotonic()
        output = model.punctuate(raw)
        elapsed = 1000 * (time.monotonic() - started)
        timings.append(elapsed)
        print(f"INPUT:      {raw}")
        print(f"HORIZON:    {output}")
        print(f"LATENCY_MS: {elapsed:.1f}\n")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"RESULT: PASS; cases={len(timings)}")
    print(f"Median latency: {statistics.median(timings):.2f} ms")
    print(f"Process peak RSS: {peak:.1f} MiB")
    print("NOTE: pilot only; compare punctuation to a manually verified reference.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
