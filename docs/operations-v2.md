# Operations V2 — GitHub App export (privacy-first)

## Scope

Operations V2 adds an owner-only preview and confirmation workflow for
technical diagnostic GitHub Issues. Source repository 0w1Max/voqelis is
PUBLIC. Issues must go ONLY to the private 0w1Max/voqelis-ops repository.
Export is OFF by default. No report is sent without explicit owner action.
A guest tester may submit local reports but cannot export them.

The Issue body is strictly allowlisted:
- random opaque export marker (does not contain user or report identifiers);
- 40-character Git SHA (or "unknown"), UTC calendar date and time-window size;
- validated event counts and predefined classified error codes.

Never transfer user IDs/names, comments, messages, notes, Day Facts,
Planner content, audio, transcripts, raw logs, filenames, stack traces,
arbitrary exception text, credentials or tokens. The owner previews the exact
payload. Free-text descriptions remain ONLY in the local SQLite database.

## Workflow and duplicate prevention

Owner: /ops -> Latest reports -> open report -> Export preview.
Preview creates a durable draft with a single-use confirmation token,
valid for 10 minutes. A confirmed callback atomically changes status from
pending to sending, then performs the HTTP request outside the transaction.

A confirmed 201 response records the numeric Issue ID. A timeout, unexpected
response, 5xx or rate limit results in uncertain status. An abrupt restart
during sending is also treated as uncertain. NO blind retry is allowed.
The owner may reconcile by searching GitHub for the exact opaque marker.
An absent search result does not prove creation failed because indexing
may be delayed; uncertain must remain blocked until resolved.

Exports are persisted in a new ops_exports table; existing V1 data and
permissions remain intact. The ops_events table gains an allowlisted
error_code field, default NONE, through an additive SQLite migration.

## GitHub App configuration (separate user action after code review)

Create a personal GitHub App voqelis-ops-reporter and install it ONLY
on the private repository 0w1Max/voqelis-ops.

Permissions:
- Issues: Read and write.
- Metadata: Read-only (intrinsic to GitHub App).
- All other permissions: No access.
- Webhooks: Disabled.

Do NOT create a general-purpose personal access token. The code requests
an installation token additionally scoped to repository voqelis-ops and
permission issues=write. Token and JWT are never logged or saved.

The GitHub App private key must be stored in a 0600, non-symlinked, regular
file owned by Linux user voqelis, not inside the public repository or chat.
Suggested path: /var/lib/voqelis/credentials/github-app.pem.

Only after safe installation and separate user approval, set in the VPS
EnvironmentFile:
- OPERATIONS_GITHUB_EXPORT_ENABLED=true
- OPERATIONS_GITHUB_APP_ID=<numeric GitHub App ID>
- OPERATIONS_GITHUB_INSTALLATION_ID=<numeric installation ID>
- OPERATIONS_GITHUB_KEY_FILE=/var/lib/voqelis/credentials/github-app.pem

The feature is disabled by default, even if IDs exist in the environment.
A controlled restart is required for configuration changes.

PyJWT[crypto]==2.10.1 is a new dependency for RSA-SHA256 GitHub App
authentication. Install and benchmark resource impact before any VPS restart.

## Release gates

- Tests: existing V1 remains functional; zero unauthorized callbacks;
  no user content in Issue preview or outgoing body.
- Migration: backup all SEVEN SQLite databases; rehearse on copies; verify
  integrity and existing report IDs, approvals and contents.
- Network failures: test timeout, 403, 429, 5xx, crash during sending,
  token replay, concurrent callbacks and reconciliation without duplicates.
- CI network requests must be mocked; no test creates real Issues.
- Merge, GitHub App setup, and deployment each require user approval.
- Friend-to-owner V1 end-to-end test is still pending and independent.
