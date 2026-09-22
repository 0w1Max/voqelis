# Voqelis

**Voqelis** is a local-first Telegram bot that turns voice messages and audio files into text using `faster-whisper` running on your own VPS.

The MVP is intentionally small and inexpensive: Telegram is the transport layer, speech recognition runs locally, and there is no paid speech-to-text API.

The long-term goal is larger than transcription: the same pipeline will later feed AI modules for tasks, daily notes, feelings, summaries, plans, reports, DOCX/Markdown export, and other structured outputs.

## Current MVP

- Telegram voice messages
- Telegram audio files
- Audio documents such as MP3, M4A, OGG/Opus, WAV and FLAC
- Local speech-to-text with `faster-whisper`
- CPU + INT8 profile for a small VPS
- One transcription worker to protect a shared 1-vCPU server
- Bounded queue and per-user back-pressure
- Telegram user allowlist
- Size and duration limits
- Temporary audio cleanup after processing
- Plain-text transcript delivery
- `systemd` service with hardening for a VPS shared with other software

## Architecture

```text
                    Telegram
                       │
          voice / audio / document
                       │
                       ▼
              ┌─────────────────┐
              │ Telegram layer  │
              └────────┬────────┘
                       │
                       ▼
              access + size checks
                       │
                       ▼
              ┌─────────────────┐
              │ bounded queue   │
              └────────┬────────┘
                       │
                    1 worker
                       │
                       ▼
              ┌─────────────────┐
              │ faster-whisper  │
              │ CPU / INT8      │
              └────────┬────────┘
                       │
                       ▼
              TranscriptionResult
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Tasks         Diary       Analyzer
          │            │            │
          └────────────┼────────────┘
                       ▼
                DOCX / Markdown / JSON
```

The important boundary is `TranscriptionResult`: future AI features should consume this contract instead of being coupled directly to Telegram or Whisper.

## Requirements

- Ubuntu/Linux
- Python 3.10–3.14
- VPS target: 1 vCPU / 2 GB RAM
- outbound HTTPS access for Telegram and the initial model download
- no system FFmpeg package is required; audio decoding is provided through PyAV

Pinned runtime dependencies:

- `aiogram==3.31.0`
- `faster-whisper==1.2.1`
- `av==18.1.0`
- `python-dotenv==1.2.3`

## Create the Telegram bot

Open **@BotFather** in Telegram and run `/newbot`.

Recommended public bot name:

```text
Voqelis
```

Recommended username:

```text
VoqelisBot
```

Telegram bot usernames must be 5–32 characters, use Latin letters/numbers/underscores, and end with `bot`. The username cannot be changed later, so choose it carefully. The token returned by BotFather is a secret and must never be committed to Git. See the official Telegram bot documentation.

## Local setup

```bash
git clone https://github.com/YOUR_USERNAME/voqelis.git
cd voqelis

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .

cp .env.example .env
```

Edit `.env` and put in the BotFather token:

```dotenv
BOT_TOKEN=123456789:AA-your-real-token
ALLOWED_USER_IDS=
```

Run the bot:

```bash
PYTHONPATH=src python -m voqelis
```

On first startup, `faster-whisper` downloads the selected model into `MODEL_CACHE_DIR`.

### First access setup

For security the allowlist is fail-closed.

1. Start the bot.
2. Send `/id` to it.
3. Copy your numeric Telegram user ID.
4. Put the ID into `.env`.
5. Restart the bot.

Example:

```dotenv
ALLOWED_USER_IDS=123456789
```

Multiple users can be listed with commas.

## VPS deployment

The production deployment runs Voqelis as a dedicated unprivileged service user.

Assuming the project will live at `/opt/voqelis`:

```bash
sudo useradd --system --home /var/lib/voqelis --shell /usr/sbin/nologin voqelis
sudo mkdir -p /opt/voqelis
sudo chown -R voqelis:voqelis /opt/voqelis
```

Copy the project into `/opt/voqelis`, then:

```bash
cd /opt/voqelis

sudo -u voqelis python3 -m venv .venv
sudo -u voqelis .venv/bin/pip install --upgrade pip
sudo -u voqelis .venv/bin/pip install -r requirements.txt
sudo -u voqelis .venv/bin/pip install -e .

sudo cp .env.example .env
sudo chown voqelis:voqelis .env
sudo chmod 600 .env
```

Edit the environment file:

```bash
sudo nano /opt/voqelis/.env
```

At minimum:

```dotenv
BOT_TOKEN=your-real-token-from-BotFather
ALLOWED_USER_IDS=your-telegram-user-id
```

Install the systemd unit:

```bash
sudo cp voqelis.service /etc/systemd/system/voqelis.service
sudo systemctl daemon-reload
sudo systemctl enable --now voqelis
```

Check the service:

```bash
sudo systemctl status voqelis --no-pager
```

Follow logs:

```bash
sudo journalctl -u voqelis -f
```

Restart after configuration/code changes:

```bash
sudo systemctl restart voqelis
```

Stop it:

```bash
sudo systemctl stop voqelis
```

## Recommended VPS profile

Start with the conservative profile already present in `.env.example`:

```dotenv
MODEL_SIZE=base
MODEL_DEVICE=cpu
MODEL_COMPUTE_TYPE=int8
CPU_THREADS=1
BEAM_SIZE=5
LANGUAGE=ru
```

Do not immediately switch to `small`. Measure actual RAM and processing time on the real VPS first. The upstream `faster-whisper` benchmark reports about 1.48 GB RAM for the **small** model in CPU INT8 on an 8-thread desktop benchmark; the actual footprint on your VPS will differ, but that number illustrates why the 2 GB machine needs a conservative starting profile.

## Input limits

The bot enforces:

- maximum Telegram file download: 20 MB
- default maximum audio duration: 20 minutes
- maximum reserved jobs globally: 4
- maximum reserved jobs per allowed user: 2

These limits are intentional because the bot shares the VPS with other workloads.

## Commands

```text
/start   help
/id      show your Telegram user ID
/status  show queue/model limits
```

## Privacy model

Audio is stored temporarily on the VPS only for processing and is deleted after the job finishes. No paid speech-recognition API is used.

Telegram remains the transport layer, so the original voice/audio necessarily passes through Telegram before the bot can download it.

## Development

Install development dependencies:

```bash
python3 -m pip install -r requirements-dev.txt
```

Run tests:

```bash
PYTHONPATH=src pytest -q
```

Run lint:

```bash
ruff check .
```

## Repository structure

```text
voqelis/
├── src/voqelis/
│   ├── bot.py
│   ├── audio.py
│   ├── config.py
│   ├── domain.py
│   ├── main.py
│   ├── queue.py
│   ├── text.py
│   └── transcription.py
├── tests/
├── data/.gitkeep
├── .env.example
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── voqelis.service
├── AUDIT.md
├── SECURITY.md
└── README.md
```

## Security

Read [SECURITY.md](SECURITY.md) before exposing the bot to additional users.

The original implementation review and architectural changes are documented in [AUDIT.md](AUDIT.md).

## License

No license is included yet because the licensing terms of the original source material should be confirmed before publishing the repository publicly.
