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


# Voice-only separators are specific to the active section. Allow a stray
# punctuation mark inserted by ASR formatting between the two spoken words.
_VOICE_MARKERS = {
    "facts": re.compile(
        r"\s*\bследующий[.!?,;:…]?\s+факт\b[.!?,;:…]?\s*",
        flags=re.IGNORECASE,
    ),
    "judgments": re.compile(
        r"\s*\bследующее[.!?,;:…]?\s+суждение\b[.!?,;:…]?\s*",
        flags=re.IGNORECASE,
    ),
}


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


def split_voice_items(text: str, *, field: str | None = None) -> tuple[str, ...]:
    """Treat a voice message as one item unless the speaker explicitly separates it.

    Automatic punctuation is only a *presentation hint*, not an instruction to
    split a personal diary entry. Spoken markers are deliberate separators;
    malformed leading/trailing markers fail closed rather than losing words.
    """
    source = text.strip()
    if not source or len(source) > MAX_INPUT_CHARS:
        raise ValueError("Text must contain 1–12000 characters")
    if field is not None and field not in _VOICE_MARKERS:
        raise ValueError("Unknown Day Facts section")
    marker = _SPOKEN_MARKER if field is None else _VOICE_MARKERS[field]
    if not marker.search(source):
        return (source,)
    parts = tuple(part.strip() for part in marker.split(source))
    if any(not part for part in parts):
        raise ValueError("Each spoken item must contain text")
    return parts


def split_explicit(text: str) -> tuple[str, ...]:
    """User-provided ' | ' delimiters override missing ASR sentence boundaries."""
    pieces = [part.strip() for part in text.split("|")]
    if len(pieces) < 2 or any(not p for p in pieces):
        raise ValueError("Specify at least two nonempty parts separated by |")
    if any(len(part) > MAX_INPUT_CHARS for part in pieces):
        raise ValueError("Part too long")
    return tuple(pieces)
