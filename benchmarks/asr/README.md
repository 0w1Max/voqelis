# Voqelis ASR benchmark

This benchmark is intentionally separate from the production runtime.

## Manifest

Create a JSON file with a fixed reference date and one-task audio clips:

```json
{
  "today": "2026-10-08",
  "cases": [
    {
      "id": "day_after_tomorrow",
      "audio": "audio/day_after_tomorrow.wav",
      "reference": "послезавтра в 13 часов сделать тест распознавания"
    }
  ]
}
```

The benchmark uses the existing Voqelis evidence recognizer to calculate critical-intent accuracy. It does not introduce a second temporal parser.

## Models

Default comparison:

- `whisper-base`
- `gigaam-v3-rnnt-int8`
- `gigaam-v3-ctc-int8`

Optional candidates include Whisper Small and GigaAM E2E variants.

Each model runs in a separate process so peak RSS is measured per model.

## Isolated environment

Use a dedicated virtual environment. Do not install ONNX ASR dependencies into the production Voqelis environment.

Example:

```bash
python3 -m venv /opt/voqelis/.venv-asr-benchmark
sudo -u voqelis /opt/voqelis/.venv-asr-benchmark/bin/pip install --upgrade pip
sudo -u voqelis /opt/voqelis/.venv-asr-benchmark/bin/pip install "onnx-asr[cpu,hub]==0.12.0"
```

Run from the repository checkout:

```bash
sudo -u voqelis env HF_HOME=/var/lib/voqelis/models/asr-benchmark \
  /opt/voqelis/.venv-asr-benchmark/bin/python asr_benchmark.py \
  --manifest /path/to/manifest.json \
  --output /path/to/result.json
```

The existing production `.venv` remains untouched.
