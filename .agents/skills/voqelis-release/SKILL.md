---
name: voqelis-release
description: Prepare and verify a separately authorized Voqelis VPS deployment with exact SHA, SQLite backups, staged tests, controlled service restart and honest readiness reporting. Use only for requested release planning, DEPLOY or post-release checks.
---

# Voqelis controlled production release

First read [AGENTS.md](../../../AGENTS.md), [terminal-script-standard.md](../../../docs/terminal-script-standard.md) and the current [CI workflow](../../../.github/workflows/ci.yml). Those files, not this skill, define global safety and Bash formatting.

## Permission boundary

A successful PR or merge does **not** authorize production. Require explicit user approval for the named deployment target. Do not interpret permission to prepare a plan as permission to execute it.

## Phases

1. **Lock identity and scope.** Record target integration SHA, user-approved feature set, expected production base SHA, affected schema, runtime configuration and named service. Compare live VPS evidence with expectations; stop on a dirty tree, unexpected HEAD or branch, changed dependencies or unrelated commits.
2. **Preflight without changing production.** Check space, ownership, executables, service health and model readiness. Perform pytest/version checks with the process working directory set to `/opt/voqelis` (never an inaccessible administrator home). Verify whether a previously installed, reviewed release runner exists before drafting one.
3. **Protect data.** Inventory all actual SQLite files and expected module databases. The established set includes `planner`, `notes`, `day_facts`, `access`, `navigation`, `todo` and `operations`, each with `.sqlite3`. Take a fresh permission-restricted SQLite API backup; verify backup integrity and checksums. Do not copy an active WAL database as an uncoordinated raw file.
4. **Rehearse and test.** Run the exact target code in a disposable directory/checkout with a suitable environment. Rehearse schema changes on isolated database copies and compare legacy counts/integrity before the real release. Abort before mutation on any failed gate.
5. **Apply only what was approved.** Allow fast-forward only to the verified target SHA; avoid reset/clean/force push. Restart `voqelis.service` only after checks and backup pass. Do not change `.env`, unrelated VPN or other services as a side effect.
6. **Verify independently.** Confirm on-disk HEAD, running service PID and restart count, ASR readiness, Telegram polling, all database integrity checks and relevant logs. Record the new backup location. Telegram end-to-end user acceptance remains a distinct check.
7. **Report actual state.** Include the SHA, checks performed, data status, services, failures and remaining actions. If execution failed after partial changes, describe that exact condition; never claim a rollback was automatic unless implemented and tested.

## Execution environment

- With no approved SSH/terminal capability, provide a reviewed single-block script following the terminal standard, then assess user-provided output. **Do not claim it ran.**
- With direct execution capability, verify that it is an authorized VPS session before taking any action.
- A GitHub merge and a production release are two separate operations, each with its own authorization.
