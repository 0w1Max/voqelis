# Voqelis · Факты дня (V1)

Independent daily reflection journal. The database lives at
\`/var/lib/voqelis/day_facts.sqlite3\`; it **never modifies Planner or Notes
tables**. Authorized Telegram users can create multiple situations per date.

## Dialog

1. \`/facts\` — begins or resumes a draft for the current Moscow date.
2. Say/write the **event**. One event is stored per situation.
3. Say/write **facts**. Each sentence ending in \`.\`, \`?\`, \`!\`, or \`…\`
   is proposed as a separate numbered item. Commas alone do not split.
4. Tap **Далее** (or \`/facts_next\`) to begin **ошибочные суждения**.
5. Say/write self-reported judgments; they are likewise numbered.
6. Tap **Далее**, check all three fields, correct where needed, then
   tap **Сохранить**. No automatic diagnosis or interpretation occurs.

If punctuation misses a boundary, use:

\`/facts_split facts 1 | Друг общался раздражённо | Мы разошлись по домам\`

For replacing a numbered item:

\`/facts_edit judgments 1 | Я как-то не так общаюсь.\`

The same commands work in the review stage. \`event 1\` selects the event.
Use \`/facts_cancel\` to discard an unfinished draft.

## Browse and export

- \`/facts_history\` — recent dates with entry counts
- \`/facts_day\` or \`/facts_day 2026-10-26\`
- \`/facts_export word\` — editable landscape A4 DOCX
- \`/facts_export pdf\` — three-column landscape A4 PDF
- \`/facts_export mobile\` — portrait A5 PDF with vertically stacked fields

Optional export date: \`/facts_export mobile 2026-10-26\`.
Documents are sent only to the requesting authorized Telegram account.
Users must forward them to anyone else themselves.

The source transcript is preserved separately in \`raw_inputs_json\`. Voice
recognition and RUPunct's sentence breaks are **proposals only**: a confirmed
draft is required before inserting the final entry. The bot neither evaluates
whether a judgment is medically 'erroneous' nor changes user statements.

## Safety and validation

Drafts and entries are user-isolated. The active draft and callback/voice
tokens are held in SQLite so stale jobs cannot write a different step. Export
files live briefly in the existing configured temporary directory and are
deleted after Telegram upload. No content is written to application logs.

Use \`python -m pytest -q tests/test_day_facts.py\` before deploying.
To check the real VPS filesystem and export dependencies without touching
production databases:

\`\`\`bash
PYTHONPATH=src /opt/voqelis/.venv/bin/python \
  -m tools.day_facts_smoke --work-dir /var/lib/voqelis
\`\`\`
