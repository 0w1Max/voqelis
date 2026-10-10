import io
import json

import voqelis.asr_worker as asr_worker


def test_worker_keeps_json_stdout_protocol_and_reuses_loaded_models(monkeypatch) -> None:
    class FakeSegment:
        def __init__(self, start: float, end: float, text: str) -> None:
            self.start = start
            self.end = end
            self.text = text

    class FakeModel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def with_vad(self, vad, **kwargs):
            assert vad is not None
            assert kwargs == {"min_silence_duration_ms": 600, "batch_size": 1}
            return self

        def with_timestamps(self):
            return self

        def recognize(self, audio_path: str):
            self.calls.append(audio_path)
            return [FakeSegment(0.0, 0.5, "распознанный текст")]

    model = FakeModel()
    vad = object()
    monkeypatch.setattr(asr_worker, "load_gigaam_model", lambda name: model)
    monkeypatch.setattr(asr_worker, "load_gigaam_vad", lambda: vad)
    input_stream = io.StringIO(
        '{"audio_path":"/tmp/one.wav"}\n'
        '{"audio_path":"/tmp/two.wav"}\n'
    )
    output_stream = io.StringIO()

    result = asr_worker.serve(
        "gigaam-v3-rnnt-int8",
        input_stream=input_stream,
        output_stream=output_stream,
    )

    rows = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert result == 0
    assert rows == [
        {"ready": True, "model": "gigaam-v3-rnnt-int8", "vad": "silero"},
        {"ok": True, "segments": [{"start": 0.0, "end": 0.5, "text": "распознанный текст"}]},
        {"ok": True, "segments": [{"start": 0.0, "end": 0.5, "text": "распознанный текст"}]},
    ]
    assert model.calls == ["/tmp/one.wav", "/tmp/two.wav"]


def test_worker_rejects_unsupported_model_without_loading(monkeypatch) -> None:
    def unexpected_load(name: str):
        raise AssertionError(f"Must not load unsupported model {name}")

    monkeypatch.setattr(asr_worker, "load_gigaam_model", unexpected_load)
    monkeypatch.setattr(
        asr_worker,
        "load_gigaam_vad",
        lambda: (_ for _ in ()).throw(AssertionError("VAD must not load")),
    )
    output_stream = io.StringIO()

    result = asr_worker.serve(
        "unknown-model",
        input_stream=io.StringIO(""),
        output_stream=output_stream,
    )

    assert result == 2
    assert json.loads(output_stream.getvalue()) == {
        "ready": False,
        "error": "Unsupported GigaAM model: 'unknown-model'",
    }


def test_worker_returns_recognition_errors_as_protocol_messages(monkeypatch) -> None:
    class FakeModel:
        def with_vad(self, vad, **kwargs):
            return self

        def with_timestamps(self):
            return self

        def recognize(self, audio_path: str):
            raise ValueError("broken audio")

    monkeypatch.setattr(asr_worker, "load_gigaam_model", lambda name: FakeModel())
    monkeypatch.setattr(asr_worker, "load_gigaam_vad", lambda: object())
    input_stream = io.StringIO('{"audio_path":"/tmp/broken.wav"}\n')
    output_stream = io.StringIO()

    result = asr_worker.serve(
        "gigaam-v3-ctc-int8",
        input_stream=input_stream,
        output_stream=output_stream,
    )

    rows = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert result == 0
    assert rows[0] == {"ready": True, "model": "gigaam-v3-ctc-int8", "vad": "silero"}
    assert rows[1]["ok"] is False
    assert rows[1]["error"] == "ValueError: broken audio"
