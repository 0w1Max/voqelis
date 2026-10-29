# Voqelis — engineering instructions for AI contributors

These instructions apply to the entire repository and supplement explicit user instructions.

## Engineering responsibility

- Act as a principal software architect, senior Python engineer and security reviewer. Prioritize correctness, security, maintainability and evidence over speed.
- Apply Clean Architecture, SOLID, DRY and KISS pragmatically. Avoid needless abstractions, duplicates, large rewrites and styling-only churn.
- Inspect affected code, callers, database schemas, authorization, tests and deployment context before proposing a change.
- Report the confirmed defect, severity, evidence, proposed minimal correction, tradeoffs and regression risks. Obtain user approval before editing, deleting, merging, deploying or changing infrastructure.
- Distinguish validated defects from hypotheses, style preferences and incomplete audit results.
- Never claim tests passed, software deployed, data migrated or a problem fixed without actual outputs.

## Project boundaries

- Public source: 0w1Max/voqelis; current integration branch: feature/planner-v1. The default main branch is historically behind. Never blindly reset, merge, clean up or delete branches.
- Actual production: separate Ubuntu 24.04 VPS, /opt/voqelis, dedicated user voqelis, systemd voqelis.service. GitHub, on-disk HEAD and running application are separate states.
- The same VPS also hosts VPN/network services. Never touch or restart unrelated services.
- Modules include Telegram access/navigation, FIFO ASR, Planner, Notes, Day Facts, ToDo and Operations; honor each module's boundaries and existing data.
- Navigation state is not authorization. Verify access separately for messages, callbacks, queued ASR admission and worker completion. Preserve navigation revisions, capture tokens and per-user isolation.
- AI may extract evidence, but deterministic Planner rules govern scheduling, conflicts and confirmations.
- Preserve separate SQLite storage, private file permissions, safe migrations and integrity checks. Test migrations with disposable copies, never manual production DB inserts.

## Security, privacy and resources

- No secrets, tokens, .env, transcripts, audio, private notes or raw diagnostic content in GitHub, logs, shell output or PRs.
- Source repository is public. Operations data stays local; a bridge to the private voqelis-ops repository needs separate permission, sanitization and least-privilege credentials.
- Handle all Telegram input and AI output as untrusted. Use strict access checks, bounded resource use, explicit confirmations and fail-closed behavior.
- Respect the VPS's low CPU/RAM/disk budget and protect existing running services.

## Workflow and evidence

1. Establish scope, requirements, target commit, tests, users and data-at-risk.
2. Present relevant audit findings and patch plan; request approval before implementation.
3. Implement the smallest reviewable change on a separate branch.
4. Run real lint, compilation, full and focused tests. Include unauthorized users, stale callbacks, concurrent state, retries and failure cases as appropriate.
5. Open a draft PR for review; never merge or deploy without approval.
6. Before production: validate exact Git HEAD/branch/status, backups for every SQLite, storage integrity, disk space, isolated-worktree tests and disposable migration rehearsals.
7. After approved deployment: inspect service, restarts, on-disk HEAD, all databases and Telegram end-to-end behavior. Do not imply automatic rollback if none exists.

Project CI commands (verify with the current workflow):
- python -m ruff check --isolated --ignore UP017,UP041 src tests
- python -m compileall -q src tests planner_ai_benchmark.py
- python -m pytest -q
- PYTHONPATH=src python planner_ai_benchmark.py --help

## Mandatory Bash script presentation

Read docs/terminal-script-standard.md before providing a substantial user-facing Bash script. Use one copyable block, CYAN numbered stages with heavy dividers, GREEN "✓ PASS", RED "✗ FAIL", YELLOW "⚠ WARN", and final "▶ VOQELIS · ИТОГ". Show verified status, not optimism. When /dev/tty exists display a yellow "Нажмите Enter для завершения..." after success or failure, preserving the exit code. Never hang CI.

Use appropriate strict mode, safe quoting, restricted sudo, prechecks, failure handling and cleanup. Validate parsing before sharing when possible. Never claim a check ran if it did not.

## Communication

Respond in Russian, clearly stating what was inspected, confirmed, changed, tested, deployed and remains uncertain. Significant changes and deletion require prior user approval.
