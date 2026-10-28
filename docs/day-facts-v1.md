# Voqelis · Факты дня (V1)

Independent daily reflection journal. The database lives at
`/var/lib/voqelis/day_facts.sqlite3`; it **never modifies Planner or Notes
tables**. Authorized Telegram users can create multiple situations per date.

## Dialog

1. `/facts` — begins or resumes a draft for the current Moscow date.
2. Say/write the **event**. One event is stored per situation.
3. Each **voice message** adds one numbered fact, regardless of how many
   sentences RUPunct inserts. In a single voice message, say
   "следующий факт" **between nonempty items** to add multiple facts.
   Written text still splits on sentence-ending punctuation (`.`, `?`,
   `!`, `…`); commas alone do not split.
4. Tap **Далее** (or `/facts_next`) to begin **ошибочные суждения**.
5. Each voice message adds one self-reported judgment; saying
   "следующее суждение" between items splits that voice message.
   A marker for a different section is not a voice delimiter.
   Written judgments retain punctuation-based splitting.
6. Tap **Далее**, check all three fields, correct where needed, then
   tap **Сохранить**. No automatic diagnosis or interpretation occurs.

If punctuation misses a boundary, use:

`/facts_split facts 1 | Друг общался раздражённо | Мы разошлись по домам`

For replacing a numbered item:

`/facts_edit judgments 1 | Я как-то не так общаюсь.`

The same commands work in the review stage. `event 1` selects the event.
Use `/facts_cancel` to discard an unfinished draft.

## Browse and export

- `/facts_history` — recent dates with entry counts
- `/facts_day` or `/facts_day 2026-10-26`
- `/facts_export word` — editable landscape A4 DOCX with three columns
- `/facts_export word_mobile` — editable portrait A5 DOCX with vertically
  stacked sections and numbered items; also available as «Word для телефона»
- `/facts_export pdf` — three-column landscape A4 PDF
- `/facts_export mobile` — portrait A5 PDF with vertically stacked fields

Optional export date: `/facts_export mobile 2026-10-26`.
Documents are sent only to the requesting authorized Telegram account.
Users must forward them to anyone else themselves.

The source transcript is preserved separately in `raw_inputs_json`. Voice
recognition and RUPunct's sentence breaks are **presentation hints**, not
automatic boundaries for spoken items. A confirmed draft is required before
inserting the final entry. The bot neither evaluates
whether a judgment is medically 'erroneous' nor changes user statements.

## Safety and validation

Drafts and entries are user-isolated. The active draft and callback/voice
tokens are held in SQLite so stale jobs cannot write a different step. Export
files live briefly in the existing configured temporary directory and are
deleted after Telegram upload. No content is written to application logs.

Use `python -m pytest -q tests/test_day_facts.py` before deploying.
To check the real VPS filesystem and export dependencies without touching
production databases:

```bash
PYTHONPATH=src /opt/voqelis/.venv/bin/python \
  -m tools.day_facts_smoke --work-dir /var/lib/voqelis
```

## Day Facts menu / re-dictation (UI update)

The Telegram home keyboard displays only Day Facts, today's saved entries,
history and three exports (Word, PDF A4, phone PDF). Old Planner buttons are
hidden, but Planner and Notes handlers, data and commands remain installed.
Open /start or /facts once to replace an existing old Telegram keyboard.
If Planner is already active, `/plan_exit` exits it.

At the **facts** step, "🔄 Повторить факты" discards the complete current
numbered facts list and asks for another voice/text dictation. At the
**judgments** step, "🔄 Повторить суждения" does the same only for judgments.
Both actions also work from the final **review** step. They preserve the
event and the unrelated list, leave finalized entries untouched, and rotate
the draft token so delayed ASR messages cannot re-add discarded material.
Ordinary additional voices **append**; they do not replace by default.

"✏️ Исправить текст" provides commands for line-level editing and full-list
replacement. For example:
- `/facts_edit facts 1 | Друг общался раздражённо, был в своих мыслях.`
- `/facts_replace facts | Первый факт. Второй факт.`
- `/facts_replace judgments | Первое суждение. Второе суждение.`

The punctuation is proposed by RUPunct, not guaranteed grammatically
correct; the interface must not silently repair clinical journal content.

## Понятное исправление пунктов

1. Нажать **«✏️ Исправить»** под черновиком.
2. Выбрать раздел кнопками «Событие», «Факты», «Суждения».
3. Выбрать конкретный номер факта или суждения. Если пунктов много,
   используются кнопки страниц.
4. Отправить исправленный текст **или новое голосовое сообщение**.
   Он заменит только выбранный пункт, не добавляя новые записи и не
   удаляя остальные. Допускается отменить исправление одной кнопкой.

Текущая выбранная запись и её номер хранятся в SQLite; смена выбора
обновляет токен и отклоняет запоздавшие сообщения старого шага.
Существующий черновик мигрируется добавлением двух nullable-столбцов.
Старые команды можно использовать, но бот не показывает их обычному
пользователю. Пустая команда `/facts_edit` открывает меню выбора.
