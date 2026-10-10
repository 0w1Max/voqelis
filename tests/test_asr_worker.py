import io
import json

import voqelis.asr_worker as asr_worker

def test_worker_keeps_json_stdout_protocol_and_reuses_loaded_model(monkeypatch) -> None:
    class FakeResult:
        text = "распознанный текст"
        tokens = ["распознанный", " текст"]
        timestamps = [0.0, 0.2]

    class FakeModel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def with_timestamps(self):
            return self

        def recognize(self, audio_path: str) -> FakeResult:
            self.calls.append(audio_path)
            return FakeResult()

    model = FakeModel()
    monkeypatch.setattr(asr_worker, "load_gigaam_model", lambda name: model)
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
        {"ready": True, "model": "gigaam-v3-rnnt-int8"},
        {
            "ok": True,
            "text": "распознанный текст",
            "tokens": ["распознанный", " текст"],
            "timestamps": [0.0, 0.2],
        },
        {
            "ok": True,
            "text": "распознанный текст",
            "tokens": ["распознанный", " текст"],
            "timestamps": [0.0, 0.2],
        },
    ]
    assert model.calls == ["/tmp/one.wav", "/tmp/two.wav"]

def test_worker_rejects_unsupported_model_without_loading(monkeypatch) -> None:
    def unexpected_load(name: str):
        raise AssertionError(f"Must not load unsupported model {name}")

    monkeypatch.setattr(asr_worker, "load_gigaam_model", unexpected_load)
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
        def with_timestamps(self):
            return self

        def recognize(self, audio_path: str):
            raise ValueError("broken audio")

    monkeypatch.setattr(asr_worker, "load_gigaam_model", lambda name: FakeModel())
    input_stream = io.StringIO('{"audio_path":"/tmp/broken.wav"}\n')
    output_stream = io.StringIO()

    result = asr_worker.serve(
        "gigaam-v3-ctc-int8",
        input_stream=input_stream,
        output_stream=output_stream,
    )

    rows = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert result == 0
    assert rows[0] == {"ready": True, "model": "gigaam-v3-ctc-int8"}
    assert rows[1]["ok"] is False
    assert rows[1]["error"] == "ValueError: broken audio"
