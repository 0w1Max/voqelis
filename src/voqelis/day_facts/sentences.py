"""Conservative sentence boundary proposals; users approve or correct every item."""

from __future__ import annotations

import re

MAX_INPUT_CHARS = 12_000
# Only a terminal punctuation mark followed by whitespace creates a boundary.
# No comma, pause length, or inferred AI label is sufficient by itself.
_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")
_SPOKEN_MARKER = re.compile(
    r"\s*\b(?:следующий факт|следующее суждение)\b[.:,;]?\s*",
    flags=re.IGNORECASE,
)


def split_sentences(text: str) -> tuple[str, ...]:
    source = text.strip()
    if not source or len(source) > MAX_INPUT_CHARS:
        raise ValueError("Text must contain 1–12000 characters")
    # An explicit spoken marker can recover boundaries missed by the ASR model.
    # Both forms are only proposals; the user must confirm the review.
    return tuple(
        item
        for phrase in _SPOKEN_MARKER.split(source)
        for item in _BOUNDARY.split(phrase.strip())
        if item
    )


def split_explicit(text: str) -> tuple[str, ...]:
    """User-provided ' | ' delimiters override missing ASR sentence boundaries."""
    pieces = [part.strip() for part in text.split("|")]
    if len(pieces) < 2 or any(not p for p in pieces):
        raise ValueError("Specify at least two nonempty parts separated by |")
    if any(len(part) > MAX_INPUT_CHARS for part in pieces):
        raise ValueError("Part too long")
    return tuple(pieces)
