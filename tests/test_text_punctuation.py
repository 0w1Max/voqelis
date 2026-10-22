"""Optional punctuation integration: fail open and preserve Planner behavior."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.domain import AudioJob, TranscriptionResult
from voqelis.rupunct import render_labeled_words
from voqelis.text_punctuation import TextPunctuationProcessor, _lexical_words


def test_punctuation_backend_is_off_by_default() -> None:
    settings = SimpleNamespace(
        asr_punctuation_backend="none",
        asr_text_punctuation_model_dir=Path("/unused"),
    )
    assert TextPunctuationProcessor.from_settings(settings) is None


def test_other_gguf_backend_does_not_load_rupunct() -> None:
    settings = SimpleNamespace(
        asr_punctuation_backend="gigaam-v3-e2e-ctc-gguf",
        asr_text_punctuation_model_dir=Path("/unused"),
    )
    assert TextPunctuationProcessor.from_settings(settings) is None


def test_rupunct_backend_selects_model_dir() -> None:
    path = Path("/var/lib/voqelis/rupunct-model-v1")
    settings = SimpleNamespace(
        asr_punctuation_backend="rupunct-small-int8",
        asr_text_punctuation_model_dir=path,
    )
    processor = TextPunctuationProcessor.from_settings(settings)
    assert isinstance(processor, TextPunctuationProcessor)
    assert processor.model_dir == path
    assert processor._model is None


def test_preserves_all_words_including_clock_times() -> None:
    raw = "завтра в 21:00 ужинать потом в 22:00 читать книгу"
    predicted = render_labeled_words(raw, [
        "UPPER_O", "LOWER_O", "LOWER_DVOETOCHIE", "LOWER_O",
        "LOWER_COMMA", "LOWER_O", "LOWER_DVOETOCHIE", "LOWER_O", "LOWER_PERIOD",
    ])
    assert _lexical_words(predicted) == _lexical_words(raw)
    assert "21:00:" not in predicted and "22:00:" not in predicted


@pytest.mark.asyncio
async def test_model_loads_once_and_formats_two_messages(monkeypatch, tmp_path) -> None:
    constructed = []

    class FakeModel:
        def __init__(self, model_dir):
            assert model_dir == tmp_path
            constructed.append(self)

        def punctuate(self, text):
            return text.capitalize() + "."

    monkeypatch.setattr("voqelis.text_punctuation.OnnxRUPunct", FakeModel)
    processor = TextPunctuationProcessor(tmp_path)
    assert await processor.format("добрый день") == "Добрый день."
    assert await processor.format("новый день") == "Новый день."
    assert len(constructed) == 1


@pytest.mark.asyncio
async def test_missing_model_or_bad_inference_falls_back(monkeypatch, tmp_path) -> None:
    calls = []

    class BrokenModel:
        def __init__(self, _dir):
            calls.append("load")
            raise FileNotFoundError("model unavailable")

    monkeypatch.setattr("voqelis.text_punctuation.OnnxRUPunct", BrokenModel)
    processor = TextPunctuationProcessor(tmp_path)
    assert await processor.format("исходный текст", fallback_text="исходный, текст") == "исходный, текст"
    assert await processor.format("другой текст") == "другой текст"
    assert calls == ["load"]


@pytest.mark.asyncio
async def test_rejects_word_changes_even_if_onnx_returns_text(tmp_path) -> None:
    processor = TextPunctuationProcessor(tmp_path)
    processor._predict_sync = lambda _text: "после завтра гулять."
    assert await processor.format("послезавтра гулять") == "послезавтра гулять"
    assert processor._disabled is True


@pytest.mark.asyncio
async def test_preserves_pause_fallback_when_model_leaves_text_unchanged(tmp_path) -> None:
    processor = TextPunctuationProcessor(tmp_path)
    processor._predict_sync = lambda text: text
    assert await processor.format(
        "это тест списка для здоровья",
        fallback_text="это тест списка, для здоровья",
    ) == "это тест списка, для здоровья"


@pytest.mark.asyncio
async def test_plain_telegram_transcription_uses_punctuation_but_planner_does_not(
    monkeypatch, tmp_path
) -> None:
    from voqelis.bot import run_worker

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_chat_action(self, *_args):
            pass

        async def send_message(self, _chat_id, text, **_kwargs):
            self.messages.append(text)

    class FakeTranscriber:
        async def transcribe(self, _path):
            return TranscriptionResult(
                text="завтра в 21:00 ужинать",
                pause_aware_text="завтра, в 21:00 ужинать",
                language="ru",
                language_probability=0,
                duration_seconds=3,
                duration_after_vad_seconds=3,
                processing_seconds=0.1,
            )

    class FakePunctuator:
        def __init__(self):
            self.calls = []

        async def format(self, text, *, fallback_text):
            self.calls.append((text, fallback_text))
            return "Завтра в 21:00 ужинать."

    class FakeQueue:
        def __init__(self):
            self.count = 0

        async def get(self):
            self.count += 1
            if self.count > 1:
                raise asyncio.CancelledError
            return AudioJob(
                chat_id=1, reply_to_message_id=2, user_id=3,
                file_path=tmp_path / "voice.ogg"
            )

        async def release(self, _user_id):
            pass

        def task_done(self):
            pass

    monkeypatch.setattr("voqelis.bot.probe_duration_seconds", lambda _path: 3.0)
    (tmp_path / "voice.ogg").write_bytes(b"test")
    bot = FakeBot()
    processor = FakePunctuator()
    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            bot=bot,
            queue=FakeQueue(),
            transcriber=FakeTranscriber(),
            settings=SimpleNamespace(max_audio_seconds=90),
            punctuation_processor=processor,
        )
    assert bot.messages == ["Завтра в 21:00 ужинать."]
    assert processor.calls == [(
        "завтра в 21:00 ужинать", "завтра, в 21:00 ужинать"
    )]
