"""Conservative sentence boundary proposals; users approve or correct every item."""

from __future__ import annotations

import re

MAX_INPUT_CHARS = 12_000
# Only a terminal punctuation mark followed by whitespace creates a boundary.
# No comma, pause length, or inferred AI label is sufficient by itself.
_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(text: str) -> tuple[str, ...]:
    source = text.strip()
    if not source or len(source) > MAX_INPUT_CHARS:
        raise ValueError("Text must contain 1–12000 characters")
    return tuple(part for part in _BOUNDARY.split(source) if part)


def split_explicit(text: str) -> tuple[str, ...]:
    """User-provided ' | ' delimiters override missing ASR sentence boundaries."""
    pieces = [part.strip() for part in text.split("|")]
    if len(pieces) < 2 or any(not p for p in pieces):
        raise ValueError("Specify at least two nonempty parts separated by |")
    if any(len(part) > MAX_INPUT_CHARS for part in pieces):
        raise ValueError("Part too long")
    return tuple(pieces)
