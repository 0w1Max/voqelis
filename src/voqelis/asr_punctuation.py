"""Optional GGUF punctuation subprocess adapter."""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from .punctuation_projection import project_punctuation

logger = logging.getLogger(__name__)
_PUNCTUATION_INFERENCE_TIMEOUT_SECONDS = 30


async def apply_gigaam_punctuation(
    *,
    audio_path: Path,
    rnnt_text: str,
    fallback_text: str | None,
    backend: str,
    model_path: Path,
    worker_python: Path,
    worker_path: Path,
    repo_root: Path,
    temp_dir: Path,
) -> str:
    """Project punctuation onto RNNT words; fail open to the RNNT text."""
    fallback = rnnt_text if fallback_text is None else fallback_text
    if (
        backend != "gigaam-v3-e2e-ctc-gguf"
        or not rnnt_text.strip()
    ):
        return fallback

    required_paths = (
        model_path,
        worker_python,
        worker_path,
    )
    if not all(path.is_file() for path in required_paths):
        logger.warning(
            "GigaAM E2E punctuation unavailable: a required file is missing; "
            "preserving RNNT text"
        )
        return fallback

    output_path = (
        temp_dir
        / f"voqelis-punctuation-{uuid.uuid4().hex}.json"
    )
    process: asyncio.subprocess.Process | None = None
    wait_task: asyncio.Task[int] | None = None

    try:
        process = await asyncio.create_subprocess_exec(
            str(worker_python),
            str(worker_path),
            "--model",
            str(model_path),
            "--audio",
            str(audio_path),
            "--output",
            str(output_path),
            cwd=str(repo_root),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(temp_dir),
                "PYTHONUNBUFFERED": "1",
                "TRANSCRIBE_BACKEND": "cpu",
            },
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # asyncio.wait leaves the process-wait task intact on timeout.
        wait_task = asyncio.create_task(process.wait())
        done, _ = await asyncio.wait(
            {wait_task},
            timeout=_PUNCTUATION_INFERENCE_TIMEOUT_SECONDS,
        )
        if not done:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await wait_task
            logger.warning(
                "GigaAM E2E punctuation timed out; preserving RNNT text"
            )
            return fallback

        if wait_task.result() != 0:
            logger.warning(
                "GigaAM E2E punctuation failed (exit=%s); preserving RNNT text",
                process.returncode,
            )
            return fallback

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            logger.warning(
                "GigaAM E2E punctuation returned invalid data; "
                "preserving RNNT text"
            )
            return fallback

        e2e_text = payload.get("text")
        if not isinstance(e2e_text, str) or not e2e_text.strip():
            logger.warning(
                "GigaAM E2E punctuation returned empty text; "
                "preserving RNNT text"
            )
            return fallback

        projection = project_punctuation(e2e_text, rnnt_text)
        logger.info(
            "GigaAM E2E punctuation projection: applied=%s reason=%s "
            "matched=%d source_tokens=%d target_tokens=%d "
            "source_coverage=%.2f target_coverage=%.2f",
            projection.applied,
            projection.reason,
            projection.matched_tokens,
            projection.source_tokens,
            projection.target_tokens,
            projection.source_coverage,
            projection.target_coverage,
        )
        return projection.text if projection.applied else fallback

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - optional stage must fail open
        logger.warning(
            "GigaAM E2E punctuation error (%s); preserving RNNT text",
            type(exc).__name__,
        )
        return fallback
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if wait_task is not None and not wait_task.done():
            await wait_task
        output_path.unlink(missing_ok=True)
