from __future__ import annotations


def chunk_text(text: str, max_chars: int = 3800) -> list[str]:
    """Split Telegram text safely without exceeding its message limit.

    We prefer paragraph/newline/space boundaries, but fall back to a hard split
    when the input contains one extremely long token.
    """
    normalized = text.strip()
    if not normalized:
        return []
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")

    chunks: list[str] = []
    remaining = normalized

    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars

        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks
