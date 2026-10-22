"""Optional ONNX text punctuation, applied only to Telegram-facing transcripts."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path

from .config import Settings
from .rupunct import OnnxRUPunct

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)
_TIMEOUT_SECONDS = 5.0


def _lexical_words(text: str) -> list[str]:
    return [match.group().casefold() for match in _WORD.finditer(text)]


class TextPunctuationProcessor:
    """One lazily loaded CPU ONNX session, disabled unless explicitly configured."""

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self._model: OnnxRUPunct | None = None
        self._thread_lock = threading.Lock()
        self._disabled = False

    @classmethod
    def from_settings(cls, settings: Settings) -> TextPunctuationProcessor | None:
        if settings.asr_punctuation_backend != "rupunct-small-int8":
            return None
        return cls(settings.asr_text_punctuation_model_dir)

    def _predict_sync(self, text: str) -> str:
        with self._thread_lock:
            if self._disabled:
                return text
            if self._model is None:
                self._model = OnnxRUPunct(self.model_dir)
                logger.info("RUPunct_small INT8 punctuation model loaded")
            return self._model.punctuate(text)

    async def format(self, text: str, *, fallback_text: str | None = None) -> str:
        fallback = text if fallback_text is None else fallback_text
        if not text.strip() or self._disabled:
            return fallback
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._predict_sync, text),
                timeout=_TIMEOUT_SECONDS,
            )
            if _lexical_words(result) != _lexical_words(text):
                raise ValueError("punctuation model changed lexical tokens")
            return result if result != text else fallback
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - optional stage must fail open
            self._disabled = True
            logger.warning(
                "RUPunct punctuation disabled after %s; preserving ASR text",
                type(exc).__name__,
            )
            return fallback
