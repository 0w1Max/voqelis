from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_WORD_RE = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)
_PUNCTUATION_RE = re.compile(r"[,.;:!?…]+|[—–-]+", re.UNICODE)

_MIN_SOURCE_COVERAGE = 0.65
_MIN_TARGET_COVERAGE = 0.55


@dataclass(frozen=True, slots=True)
class PunctuationProjection:
    text: str
    applied: bool
    source_tokens: int
    target_tokens: int
    matched_tokens: int
    source_coverage: float
    target_coverage: float
    reason: str


def _normalize_token(token: str) -> str:
    return token.casefold().replace("ё", "е")


def _punctuation_after(text: str, start: int, end: int) -> str:
    return "".join(_PUNCTUATION_RE.findall(text[start:end]))


def _apply_source_case(target_token: str, source_token: str) -> str:
    if source_token.isupper() and any(char.isalpha() for char in source_token):
        return target_token.upper()
    if source_token[:1].isupper():
        return target_token[:1].upper() + target_token[1:]
    return target_token


def project_punctuation(
    e2e_text: str,
    rnnt_text: str,
) -> PunctuationProjection:
    """Project punctuation and case from E2E text without replacing RNNT words.

    Returns the original RNNT text when alignment confidence is insufficient.
    Numeric or lexical differences are never copied from the E2E transcript.
    """
    source_tokens = list(_WORD_RE.finditer(e2e_text))
    target_tokens = list(_WORD_RE.finditer(rnnt_text))
    source_count = len(source_tokens)
    target_count = len(target_tokens)

    def fallback(reason: str, matched: int = 0) -> PunctuationProjection:
        source_coverage = matched / source_count if source_count else 0.0
        target_coverage = matched / target_count if target_count else 0.0
        return PunctuationProjection(
            text=rnnt_text,
            applied=False,
            source_tokens=source_count,
            target_tokens=target_count,
            matched_tokens=matched,
            source_coverage=source_coverage,
            target_coverage=target_coverage,
            reason=reason,
        )

    if not source_tokens or not target_tokens:
        return fallback("empty token sequence")

    matcher = difflib.SequenceMatcher(
        None,
        [_normalize_token(match.group()) for match in source_tokens],
        [_normalize_token(match.group()) for match in target_tokens],
        autojunk=False,
    )

    target_to_source: dict[int, int] = {}
    matched_count = 0
    for block in matcher.get_matching_blocks():
        matched_count += block.size
        for offset in range(block.size):
            target_to_source[block.b + offset] = block.a + offset

    source_coverage = matched_count / source_count
    target_coverage = matched_count / target_count
    minimum_matches = min(3, source_count, target_count)

    if matched_count < minimum_matches:
        return fallback("too few matching tokens", matched_count)

    if target_to_source.get(0) != 0:
        return fallback("first token does not align", matched_count)

    if target_to_source.get(target_count - 1) != source_count - 1:
        return fallback("last token does not align", matched_count)

    if source_coverage < _MIN_SOURCE_COVERAGE:
        return fallback("source alignment coverage too low", matched_count)

    if target_coverage < _MIN_TARGET_COVERAGE:
        return fallback("target alignment coverage too low", matched_count)

    parts = [rnnt_text[: target_tokens[0].start()]]
    capitalize_next = False

    for target_index, target_match in enumerate(target_tokens):
        target_word = target_match.group()
        capitalize_current = capitalize_next
        capitalize_next = False
        source_index = target_to_source.get(target_index)

        source_marks = ""
        source_gap = ""
        if source_index is not None:
            source_match = source_tokens[source_index]
            target_word = _apply_source_case(target_word, source_match.group())
            source_gap_end = (
                source_tokens[source_index + 1].start()
                if source_index + 1 < source_count
                else len(e2e_text)
            )
            source_gap = e2e_text[source_match.end() : source_gap_end]
            source_marks = _punctuation_after(
                e2e_text,
                source_match.end(),
                source_gap_end,
            )

        if capitalize_current and target_word:
            target_word = target_word[:1].upper() + target_word[1:]
        if any(mark in ".!?…" for mark in source_marks):
            capitalize_next = True

        target_gap_end = (
            target_tokens[target_index + 1].start()
            if target_index + 1 < target_count
            else len(rnnt_text)
        )
        target_gap = rnnt_text[target_match.end() : target_gap_end]

        parts.append(target_word)

        # Never duplicate punctuation that is already present in the RNNT text.
        if source_marks and not _PUNCTUATION_RE.search(target_gap):
            # A hyphen between the next two aligned words is a word joiner,
            # not punctuation followed by a space. Preserve this structural
            # boundary rather than producing "во- первых" or "из- за".
            joins_compound = (
                source_marks == "-"
                and source_gap == "-"
                and source_index is not None
                and source_index + 1 < source_count
                and target_index + 1 < target_count
                and target_to_source.get(target_index + 1) == source_index + 1
                and target_gap.isspace()
            )
            if joins_compound:
                parts.append("-")
                target_gap = ""
            else:
                dash_only = all(mark in "—–-" for mark in source_marks)
                leading_space = ""
                if dash_only:
                    first_mark = source_gap.find(source_marks[0])
                    if first_mark > 0 and source_gap[:first_mark].isspace():
                        leading_space = " "
                parts.append(leading_space)
                parts.append(source_marks)

        parts.append(target_gap)

    return PunctuationProjection(
        text="".join(parts),
        applied=True,
        source_tokens=source_count,
        target_tokens=target_count,
        matched_tokens=matched_count,
        source_coverage=source_coverage,
        target_coverage=target_coverage,
        reason="aligned",
    )
