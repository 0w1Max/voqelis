"""Strict data-minimized, deterministic GitHub Issue export body."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .store import Report

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
ALLOWED_ERROR_CODES = frozenset({
    "AUDIO_DECODE_FAILED", "ASR_EMPTY", "HANDLER_EXCEPTION",
    "TELEGRAM_SEND_FAILED", "TELEGRAM_RATE_LIMIT", "WORKER_EXCEPTION",
})


def build_issue_body(report: Report, marker: str, revision: str) -> str:
    """Build only from enumerated metadata; never use free-text description or user ID."""
    if not re.fullmatch(r"VQEXP-[0-9a-f]{32}", marker):
        raise ValueError("Invalid export marker")
    if report.window_minutes not in {10, 30, 60}:
        raise ValueError("Invalid diagnostic window")
    revision = revision if SHA_RE.fullmatch(revision) else "unknown"
    data = report.snapshot
    counts = {}
    for key in ("handled", "completed", "failed"):
        number = data.get(key)
        if type(number) is not int or not 0 <= number <= 50_000:
            raise ValueError("Unsafe diagnostic counter")
        counts[key] = number
    raw_codes = data.get("error_codes", {})
    if not isinstance(raw_codes, dict):
        raise TypeError("Invalid diagnostics")
    codes: list[str] = []
    for key in sorted(raw_codes):
        value = raw_codes[key]
        if key not in ALLOWED_ERROR_CODES or type(value) is not int:
            raise ValueError("Unrecognized diagnostic code")
        if not 0 <= value <= 50_000:
            raise ValueError("Unsafe diagnostic count")
        if value:
            codes.append(f"- {key}: {value}")
    date_utc = datetime.fromtimestamp(report.created_at, UTC).strftime("%Y-%m-%d")
    return "\n".join([
        "## Voqelis · anonymized technical report",
        "",
        f"Export marker: {marker}",
        f"Release SHA: {revision}",
        f"UTC date: {date_utc}",
        f"Window: {report.window_minutes} minutes",
        "",
        "### Technical counters",
        f"- Handled Telegram updates: {counts['handled']}",
        f"- Completed audio jobs: {counts['completed']}",
        f"- Observed failures: {counts['failed']}",
        "",
        "### Classified failures",
        *(codes if codes else ["- None recorded"]),
        "",
        ("No Telegram user IDs, messages, free-text comments, audio, "
         "transcripts, stack traces or credentials are included."),
    ])


def issue_title(marker: str) -> str:
    if not re.fullmatch(r"VQEXP-[0-9a-f]{32}", marker):
        raise ValueError("Invalid export marker")
    return f"Voqelis technical diagnostic · {marker}"
