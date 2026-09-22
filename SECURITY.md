# Security Notes

## Threat model

This is a personal Telegram bot running on the same VPS as networking software. The main risks are therefore resource exhaustion and accidental exposure rather than public web attacks.

### Controls

- Telegram user allowlist (`ALLOWED_USER_IDS`)
- private-chat-only processing
- 20 MB input ceiling
- configurable audio duration ceiling
- bounded job queue
- per-user queue limit
- UUID-based temporary filenames
- temporary audio deleted after processing
- stale temp files removed on startup
- bot service runs as a dedicated unprivileged user
- systemd memory/CPU controls
- systemd hardening
- no transcript/audio persistence by default
- no arbitrary shell command execution from Telegram

## Secrets

`.env` must never be committed.

The only expected secret is `BOT_TOKEN`.

## If the project becomes public/multi-user

Add:

- persistent queue
- stronger per-user rate limiting
- abuse monitoring
- durable storage policy
- explicit retention controls
- request authentication beyond Telegram account IDs for sensitive operations
- structured audit logs
