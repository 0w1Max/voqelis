---
name: voqelis-develop
description: Implement an explicitly authorized Voqelis feature or defect fix in a small reviewable branch, preserving module boundaries and adding focused regressions. Use for approved Python, Telegram UI, SQLite or workflow code changes.
---

# Voqelis development workflow

Read [AGENTS.md](../../../AGENTS.md) for architecture, permissions and security policy. Use `voqelis-context` first when the baseline or previous work is uncertain.

## Execution

1. **Convert the request to a contract.** Capture observable Telegram behavior, data persistence, negative cases, affected users, scope exclusions and acceptance tests. Confirm edit authorization before modifying code.
2. **Trace the existing path.** Inspect callers and tests from Telegram/router through service, store, worker and delivery (as applicable). Identify the owner of each decision; do not add a second route or scheduler for an existing responsibility.
3. **Choose the smallest diff.** Branch from the verified integration SHA, not an assumed default branch. Keep migrations additive and confined to the owning database. Explicitly separate independent features into PRs.
4. **Implement safety at state transitions.** For user-specific changes, verify access at entry and execution, not only at navigation. Review stale callbacks, token/revision checks, concurrency, idempotency and how a restart affects pending work.
5. **Make acceptance observable.** Add focused tests for expected behavior plus misuse, expiration, data preservation and error recovery; where the requirement is in an RFC, map it to an actual test or note the gap.
6. **Check before proposing merge.** Prefer local lint/compile/tests **before pushing** when a code-execution workspace is available; use the commands from [CI](../../../.github/workflows/ci.yml). When only GitHub editing is available, open a Draft PR, wait for real CI, and do not misrepresent CI as local preflight.
7. **Hand off a Draft PR.** Describe exact changed files, behavior, schema impact, test evidence, untested scenarios and explicit merge/deploy gates.

## Done means

A reviewable change with verifiable evidence and a clear remaining gate. A passing PR does **not** mean production is updated. Never merge or deploy under this skill alone.
