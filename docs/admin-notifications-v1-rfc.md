# RFC — Voqelis Admin Notifications V1

Status: owner-approved RFC; implementation is staged in this draft PR, never merged or deployed.

## Purpose

Provide the bot owner with a dedicated **📣 Уведомления пользователям** section
in the existing **🛡 Админ-панель**. This is an admin-to-user broadcast feature,
**not** the same thing as ToDo reminders or the user's personal daily ToDo digest.

The owner has requested two ways to compose announcements:

1. **Manual announcement:** the owner writes a new message to approved users.
2. **Important update suggestion:** Voqelis proposes a draft announcing a
   major feature/module becoming available; it may be reviewed, edited or
   rejected. Do not broadcast ordinary commits, low-impact changes, or
   diagnostic data. A successful GitHub CI run never automatically authorizes
   a public feature announcement or a production deployment claim.

**No announcement is sent without an explicit owner confirmation on the
exact previewed draft.** Confirming after an edit must bind to the new content
revision. Stale callback buttons and duplicate confirms must fail closed.

## User-facing Telegram interaction

Entry point: 🛡 Админ-панель → 📣 Уведомления пользователям

    📣 Уведомления пользователям
    [➕ Создать объявление]
    [📝 Черновики]
    [📤 История рассылок]
    [⚙️ Настройки]

Draft flow:

    ➕ Новое объявление
    Введи текст объявления. Его получат одобренные пользователи бота.
    [⬅️ Отмена]

    📝 Предпросмотр
    Заголовок: В Voqelis появился новый раздел
    Текст: …
    Получателей: N одобренных пользователей (проверить на отправке)
    [✏️ Изменить] [✅ Подтвердить рассылку]
    [🗑 Отменить]

After confirmation:

    📤 Рассылка в процессе
    Отправлено: X
    Не доставлено: Y
    Осталось: Z
    [📊 Статус] [⏸ Приостановить]

Completion: show exact sent, failed, blocked and skipped counts.
Do not imply that Telegram showing "sent" means a recipient has read the
message. Include a link back to the admin notification center.

## Recipient and access rules

- Only the single verified owner admin_id may enter draft, preview,
  confirm, pause, resume, or see delivery results. Approving a user never
  grants admin permission.
- Recipient audience: all *currently approved* users of this Voqelis bot
  who have started the bot in a private conversation and can receive direct
  messages. Exclude the owner by default, with a separate test-preview-to-self
  option. Never send to a stranger, a pending/revoked user or a group chat.
- Recipient eligibility is rechecked immediately before each send.
  A queued send must not bypass a revoked approval.
- Snapshot eligible recipient IDs at confirmation for audit and progress;
  never attach private user lists to GitHub or public logs.
- Avoid notifying users about personal diagnostic reports, ToDo tasks,
  transcripts, AI prompts, or sensitive private project information.

## Durable delivery and safety

Use an isolated SQLite outbox (e.g. admin_notifications.sqlite3) so broadcasts
cannot corrupt ToDo, Access, Navigation or Operations storage. For durability,
store draft text, revision, owner, state, created/confirmed timestamps and
delivery rows keyed by broadcast ID + recipient user ID, with a unique key to
prevent duplicate queue insertion.

States: draft → queued → sending → completed / paused / cancelled.
Per-recipient states: pending → sending → sent / failed / uncertain / skipped.
A network failure after Telegram accepted a message may have an uncertain
outcome; Bot API does not provide exactly-once semantics. No blind replay
after an uncertain outcome; show the exact count in the admin summary.

Bounded queue, a conservative rate limiter, response-specific handling of
TelegramRetryAfter, blocked/forbidden recipients, transient failures and
explicit progress tracking. Avoid floods and retry storms.

On process restart recover the durable outbox safely, without losing progress
or automatically repeating a possibly delivered message. Integrate the
worker in the existing Python service; no second polling bot or external daemon.

Include test coverage for repeated confirm clicks, edits after preview,
unauthorized buttons, user access revocation, outages, rate limits, process
crashes, cancellation/pause, and recipient count changes.

## Independence from ToDo

ToDo Reminders V2.1 and daily active-tasks digest V2.2 are scheduled
**user-specific** notifications in todo.sqlite3. Owner announcements are
a separate delivery domain with distinct permissions, recipient selection,
draft approval and rate limiting. Reuse a low-level Telegram delivery
utility only after its privacy/authorization contracts are explicit; do not
share databases, arbitrary "send to all" callbacks or state machines.

## Release gates

Implement in a **separate PR** from ToDo, behind an owner-only entry point
and with broadcasts disabled until separately approved and tested. Review
on test accounts first. Back up all applicable production SQLite files,
rehearse migrations, run full CI and explicitly authorize VPS deployment.
Neither draft creation nor CI may send real Telegram announcements.

## V1 code release controls

- Environment flag `ADMIN_ANNOUNCEMENTS_ENABLED=false` disables all broadcasts,
  but the owner can still compose/preview drafts and test the message on themselves.
- `admin_notifications.sqlite3` is a new independent eighth SQLite DB; back up
  seven existing DBs before upgrading, then verify all eight after startup.
- The actor is the unique bootstrap owner from `ALLOWED_USER_IDS`; recipients
  are rechecked against the live approved-user set at delivery time.
- An admin can send manually created announcements only after reviewing and
  confirming their exact content. Automated major-release suggestions are future
  drafts, not push notifications triggered by a GitHub merge.
- Users may opt out with `/notify_off` and restore with `/notify_on`.
  These controls do NOT touch user ToDo reminders.
- A single active send may complete during pause/cancel due to an in-flight
  Telegram request. Unknown results remain `uncertain`, without blind retry.
- No real messages are sent in CI; sender tests use a fake Bot object.
