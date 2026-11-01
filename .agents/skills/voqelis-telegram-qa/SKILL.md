---
name: voqelis-telegram-qa
description: Plan and verify Voqelis Telegram end-to-end acceptance for voice, text, callbacks, reminders, access and module navigation. Use when reviewing bot UX or verifying a specific deployed release; keep observed evidence distinct from expected behavior.
---

# Voqelis Telegram acceptance

Read [AGENTS.md](../../../AGENTS.md) for access, privacy and module boundaries. For uncertain code or deployed versions, start with `voqelis-context`. This skill converts requirements into testable Telegram interactions without modifying production.

## Prepare

1. Write down the tested feature, production SHA confirmed independently, local time and selected ToDo timezone if time behavior matters. Do not infer VPS deployment merely from a merged PR.
2. Map each relevant requirement to a case with input, expected bot response, interaction to confirm or cancel, and observable result.
3. Include both ordinary and failure scenarios where applicable: typed/voice wording, ambiguous times, wrong/past dates, cancelled drafts, stale/foreign callbacks, unapproved users, navigation changes, duplicate delivery, process restart and DST boundaries.
4. Design safe test data and a cleanup plan. Prefer a dedicated test task and manually cancel its reminders. Never require sharing real private voice recordings, Telegram tokens, personal IDs or database rows.

## Execute or guide

- When no connected Telegram test harness is available, provide a concise manual checklist with specific steps and expected outcomes. **Do not report tests as passed** unless the user has supplied actual Telegram evidence.
- For real notifications, distinguish a reminder being *saved* from a notification being *delivered at the intended time*. Inspect a sent message and its timestamp; a successful scheduler log is not sufficient.
- Verify UI paths (start, create, return, edit, cancel, reopen), authorization at action time and independent behavior of modules.
- For voice tests, distinguish the audio/transcript from the proposed structured result; if only final bot messages are visible, ASR word accuracy remains unverified.
- When verifying daily summaries or recurring actions, cover no-active-items, disabled schedules, multiple configured times, local timezone and crash/restart duplicate suppression.

## Report

Use a compact acceptance matrix: scenario, expected, observed, PASS / FAIL / NOT TESTED and evidence. Separate release-blocking defects, UX improvements and follow-up tests. Never post real Telegram chat histories or other personal data to a public PR; sanitize examples first.

This skill cannot authorize a deploy or a mass notification. Owner/admin broadcast checks are separate from personal ToDo reminders.
