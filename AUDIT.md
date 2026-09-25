# Architecture & Code Audit

## Verdict

The original implementation was a good functional prototype, but it was not yet safe enough to expose as a personal production service on a VPS shared with a VPN.

The refactored version keeps the original core idea — aiogram + faster-whisper + PyAV + one worker — and fixes the main reliability and security gaps without introducing Redis, a database, Docker, or a second service.

## Critical fixes

### 1. `.env` was documented but not loaded during manual execution

The original code read `os.environ["BOT_TOKEN"]`, while the README instructed the operator to create `.env` and launch Python directly.

That works under systemd with `EnvironmentFile`, but not in a plain shell unless the variables are exported manually.

**Fix:** `python-dotenv` loads `.env` in `load_settings()`.

### 2. Raw transcription was sent while global HTML parsing was enabled

The original bot configured `ParseMode.HTML` globally. Whisper output is untrusted plain text and can contain `<`, `>`, `&`, or malformed fragments, causing Telegram entity parsing failures.

**Fix:** transcription messages are sent with `parse_mode=None`; bot responses are plain text except for small trusted status snippets.

### 3. Incoming downloads were effectively unbounded

aiogram 3 processes updates as tasks by default. The original handler downloaded every incoming file before putting it into an unbounded queue.

A burst of messages could therefore consume disk/network resources even though only one transcription worker existed.

**Fix:** bounded queue + global reservation + per-user reservation. Files are downloaded only after a slot is reserved.

### 4. Personal bot had no access control

Anyone who discovered the bot could make the VPS spend CPU time on transcription.

**Fix:** `ALLOWED_USER_IDS` allowlist. Empty allowlist is fail-closed for transcription access. `/id` is available to identify the current Telegram account.

### 5. Telegram's 20 MB download ceiling was not checked explicitly

The original code did not reject oversize files before `get_file`/download.

**Fix:** declared size, Telegram file metadata, and actual downloaded size are checked.

### 6. Model startup failures could leave a "live but useless" bot

The original worker loaded the model before its loop. If loading failed, the worker task died while polling continued.

**Fix:** model loading happens before polling starts. If it fails, systemd sees a failed process and restarts it.

### 7. Temporary files could survive crashes

The original worker cleaned files in its normal `finally`, but files downloaded before a crash/reboot could remain.

**Fix:** dedicated temp directory cleanup on startup; every accepted job also gets `finally` cleanup.

### 8. The service was not hardened against the rest of the VPS

The original service only had `NoNewPrivileges` and `PrivateTmp`, with a relatively loose resource policy.

**Fix:** memory ceiling, OOM preference, lower CPU priority, private runtime/state directories, restricted address families and several systemd hardening directives.

## Important design decisions

### Why one worker?

The VPS has one vCPU. Parallel Whisper inference would compete for the same CPU and raise peak RAM usage. The application therefore deliberately serializes transcription.

### Why in-memory queue?

For the MVP, persistence would add another moving part. Losing queued jobs during a service restart is acceptable because the source voice message remains in Telegram and can be resent.

If the project evolves into multi-user or business use, the queue should become persistent (Redis/SQLite/PostgreSQL or a dedicated job broker).

### Why faster-whisper rather than adding an HTTP AI API?

The requirement is zero recurring AI/API cost and local processing. faster-whisper uses CTranslate2 and supports CPU INT8; PyAV bundles the FFmpeg libraries, so a system ffmpeg package is not required.

### Why `base` by default?

`small/int8` may improve quality, but its RAM footprint is a poor fit for a 2 GB VPS already running networking software. The configured profile is therefore conservative: `base + int8 + 1 CPU thread`.

After measuring real processing speed and RAM on the actual VPS, `MODEL_SIZE=small` can be tested as an opt-in change.

## Known MVP limitations

- The queue is memory-only and is lost on restart.
- There is no web dashboard.
- No persistent transcript storage is implemented.
- No speaker diarization.
- No word-level timestamps.
- No language switching command yet.
- There is no health endpoint because the bot uses long polling and systemd already supervises the process.

## Validation performed before packaging

- `python -m compileall -q src tests` — passed.
- `pytest -q` — the test suite is the release gate; the latest branch run was still in progress at packaging time.
- Production dependencies are pinned in `requirements.txt` for reproducible deployment.


## Planner V1 pre-VPS review

Before the first real server test, the planner was reviewed end-to-end against the current requirements and the supplied day-table structure. The implementation keeps deterministic scheduling separate from optional AI extraction, persists planner state in SQLite, uses stable plan-item IDs for review editing, and protects conflict confirmations against stale task positions.

Additional hardening in the latest revision:

- recurring templates must fit inside the configured planning window;
- stale Review sessions fail closed instead of raising on missing or deleted task IDs;
- full-day AI review persists the normalized, de-duplicated proposal that was actually shown to the user;
- the deployment checklist preserves the existing production .env, model cache, and planner database before replacement.

The first VPS test should be treated as a controlled smoke test, not as an assumption that every natural-language formulation is already understood. Parser/AI extraction and scheduling are deliberately separate so failures can be isolated.
