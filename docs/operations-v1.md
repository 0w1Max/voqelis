# Operations V1 — scope and safety contract

Operations V1 gives the owner and one explicitly assigned approved guest a
Telegram button to report improper bot behavior. It stores diagnostic metadata
locally in a separate SQLite database. No automatic publishing to GitHub and
no mass user notifications are part of this release.

## Access model

The owner is the unique bootstrap owner established by the existing access
store. The owner may permit **one** currently approved guest to submit reports.
This permission is replaced when a different guest is assigned and is
ineffective when main bot access is revoked. Owner-only actions: view or resolve
reports, inspect aggregate diagnostics, assign/revoke the guest reporter.
Both message and callback handlers verify the live Telegram actor ID, approved
status and private chat.

## Reporting workflow

The eligible user presses "🐞 Сообщить об ошибке" or sends /report followed
by an optional short description (up to 800 characters). The next screen
offers 10/30/60-minute windows. Choosing one creates one immutable report.
The selection token expires in 10 minutes, and each user is limited to
3 reports per hour. Repeated callbacks do not create duplicate reports.
The owner is notified in Telegram; notification delivery is best-effort
because the report is already durable.

## Data minimization

Stored event metadata: UTC timestamp, Telegram user ID, whitelisted module,
event class (message/callback/audio), execution outcome and integer elapsed ms.
Stored report data: UTC timestamp, user ID, requested time window, optional
user-provided description, aggregate counts and reviewed state.

No raw Telegram messages, voices, transcripts, note contents, daily facts,
Planner text, file paths, stack traces, tokens or AI provider keys are
automatically captured. Reports are not sent to the public repository.
A future private GitHub bridge requires a new, separately reviewed consent
and sanitization mechanism.

"handled" means a handler returned without raising an exception. It is not
proof of success of the underlying business operation. "completed" refers to
the worker completing the audio processing path, while "failed" records
exceptions, not logical inaccuracies.

## Storage and operations

The independent database operations.sqlite3 uses private filesystem mode
0600, no symlinks, bounded input and transaction-protected tokens.
Events are pruned after 7 days on every 100th event insert. Reports are pruned
after 30 days upon report creation. These are lazy retention limits, not
scheduled guaranteed expiration if activity stops. Periodic cleanup can be
added later as a separate planned task.

No new AI dependency, web service, journald reader or systemd service.
No changes to Planner, Day Facts, Notes or ToDo data models.
No deployment until independent CI + tests on VPS + full backups of all six
existing databases, followed by functional Telegram acceptance.
