# RFC — ToDo Reminders V2 (Telegram UX + reliable delivery)

Status: **accepted for implementation as phased, reviewable PRs**. This RFC is a product and technical contract; it does not deploy any code.

## Problem and goals

Today ToDo is a simple personal checklist: one text/voice input creates one
task, stored per user in todo.sqlite3. It has no deadlines, notifications or
scheduler. Keep that lightweight behavior: reminder setup must never block
task creation or silently add Planner tasks.

Two **independent** types of notifications:
1. V2.1 individual dated reminders: zero to ten distinct future reminders per
   unfinished task, set/edited/deleted independently.
2. V2.2 daily digest: all active tasks, initially enabled at 16:00 local time,
   individually configurable on/off and up to five distinct times per day.

Daily digest does NOT require a per-task reminder. Turning it off never
silences per-task reminders. Individual reminders are opt-in per task.
The first digest notification should be explained clearly at onboarding,
with an immediate control to disable it. Do not send a digest with zero
active tasks.

## User journey V2.1

Create via Telegram text or a single voice message -> task is saved
immediately -> send inline action card:

    ✅ Дело добавлено: Купить лекарства
    [🔔 Напомнить] [Не сейчас]
    [➕ Ещё дело] [📋 Активные дела]

From a task card:
    [🔔 Напоминания] [✅ Выполнено] [✏️ Изменить] [🗑 Удалить]

"Напоминания" shows all pending times, each with edit/delete actions and
[➕ Добавить напоминание]. The initial quick picker offers
[Сегодня] [Завтра] [Послезавтра], [📅 Другая дата],
[🎙 Сказать голосом] [⌨️ Написать]. Once a date is selected:
[09:00] [12:00] [16:00] [18:00] [21:00] [Другое время].
Hide past times for "today"; show full localized date & timezone at final
confirmation. Typed/voice "послезавтра в 18:30", "5 ноября в 18:30",
"05.11.2026 18:30" go straight to *confirmation*, never automatic scheduling.
Date without time requires time clarification. Reject ambiguous expressions
and nonexistent/ambiguous DST times rather than guessing. Allow user to back
out at any point. A task still exists if reminder creation is canceled.

Do not demand a date on every task creation. Done/deleted tasks cannot get
new reminders; completion/deletion cancels future pending reminders. Reopening
does not recreate passed reminders. Editing task text preserves reminders.

Use callback tokens/revisions to reject stale, replayed, foreign or expired
button clicks. Text/voice reminder capture must be separate from todo_capture
and must never feed a Planner/Notes/Day Facts handler. Recheck live access
at capture, confirmation and delivery, including worker completion.

## Time and data semantics

- Default timezone: Europe/Moscow; V2.2 supports a per-user IANA timezone.
- Store event timestamp in UTC seconds and display absolute local date, time
  and timezone. Reject past instants and duplicates. Bound future horizon to
  one year in the first release; cap at 10 future reminders per task.
- Daily digest user-specific times are a set (default 16:00).
- No silent midnight assumptions, conversion to deadlines or relocation.
- The user sees each reminder time in its original timezone.
- Message: "🔔 Напоминание\nКупить лекарства\n[✅ Выполнено] [⏰ Позже]
  [📋 Открыть дело]". "Позже" requires explicit time choice, not a silent move.
- If app was unavailable, send per-task reminder within 2 hours with a
  "с задержкой" marker; otherwise mark missed. Skip expired daily digests
  instead of sending an avalanche.

## Durability and privacy

Use the existing independent todo.sqlite3. Add additive schema for reminder
rows, drafts, per-user digest preferences and send status/outbox. Do not
migrate Planner/Notes/Day Facts. Write with SQLite transactions and indexes.
The scheduling loop belongs to existing Voqelis asyncio process, not a second
bot or new VPS daemon. Claim jobs atomically, check task active/access live,
and cap sends per cycle; avoid blocking polling or ASR on slow Telegram sends.

Telegram send_message cannot guarantee exactly-once if the network fails
after a request was received but before we saw the response. Treat ambiguous
outcomes conservatively, surface delivery state, and avoid uncontrolled retries
or duplicate storms. Keep state across restarts, include startup recovery,
bounded retries for known-safe failures, and test shutdown during a send.
Store user data locally and never include it in public GitHub diagnostics.

## Release plan

- V2.1a: RFC, deterministic Russian date/time parser, SQLite schema/state
  machine, tests; no active sends until follow-up integration is validated.
- V2.1b: Telegram inline picker, typed and voice input, one-shot confirmations,
  multiple reminders per task, persistent single-process scheduler and
  automated Telegram delivery. End-to-end Telegram acceptance is mandatory.
- V2.2: daily digest default 16:00 and per-user settings, add/remove times,
  timezone selection, pagination for long lists.
- V2.3: load/failure/operability checks and UX refinement.

Every stage: isolated branch, CI (Ruff, compileall, pytest, smoke), test
migrations on a disposable copy of seven SQLite databases, full fresh backups
before any explicitly authorized production rollout. The real VPS currently
runs older fd513280; Operations V2 merge 18fe696 is not yet deployed.
Do not mix Operations and ToDo schema upgrades in one unreviewed release.

## Acceptance tests

Per-user isolation; text/voice capture one shot; relative dates across
midnight and year rollover; leap days; DST gap/fold handling; duplicate or
past time rejected; ten-reminder cap; stale buttons; completion/deletion
cancels; reopen leaves old reminders canceled; two claimers cannot deliver
same row simultaneously; unapproved users excluded; outage/restart recovery;
messages cannot cross modules; all V1 ToDo flows remain valid.
