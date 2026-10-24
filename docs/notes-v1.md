# Notes V1 (development)

Notes is a separate Voqelis module. It has its own SQLite file,
\`notes.sqlite3\`, placed alongside the configured Planner DB; **no Planner
tables are changed**. The production \`voqelis.service\` StateDirectory already
contains that path.

## Telegram commands

- \`/note Купить молоко и хлеб\` — save text exactly as entered.
- \`/note\` — arm the next voice message for a one-time note (15 minute expiry).
- \`/note_cancel\` — cancel waiting for a voice note.
- \`/notes\` or \`/notes 2\` — list ten recent notes per page.
- \`/note_search проект\` — search saved notes case-insensitively.
- \`/note_show 3\` — display full note number 3.

Only an authorized user in a private Telegram chat may call these commands.
Notes are strictly scoped by \`user_id\`.

**Normal voice messages are never saved automatically.** Voice note intent is
captured when the file is accepted for the ASR queue. GigaAM original words are
stored in \`original_text\`; the visible body may be formatted by RUPunct.
Planner sessions take priority over voice-note capture, and creation via
\`/note\` is not allowed during an active Planner session.

Notes do not use LLM routing or infer tasks. The maximum note size is 12,000
characters; oversized notes are rejected without truncation. Note deletion,
tags, AI summaries, and cross-module linking are out of scope for V1.

## Isolated validation on VPS

After fetching the exact vetted commit into a separate worktree, run
\`python -m pytest tests/test_notes.py\` in the existing Python environment.
For an additional SQLite file-system test run:

\`\`\`bash
PYTHONPATH=src /opt/voqelis/.venv/bin/python \
  -m tools.notes_v1_smoke --work-dir /var/lib/voqelis
\`\`\`

The smoke test uses a disposable folder under \`/var/lib/voqelis\`, cleans
up afterwards, and never opens the production Planner or Notes database.
