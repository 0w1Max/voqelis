from voqelis.text import chunk_text


def test_chunk_text_keeps_content_and_respects_limit():
    text = ("Первый абзац. " * 500).strip()
    chunks = chunk_text(text, max_chars=100)

    assert chunks
    assert all(0 < len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_chunk_text_empty():
    assert chunk_text("   ") == []


def test_chunk_text_hard_splits_long_token():
    chunks = chunk_text("x" * 25, max_chars=10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]
