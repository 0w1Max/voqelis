---
name: voqelis-quality
description: Review a Voqelis branch or PR for requirement coverage, regression risk, authorization and real CI evidence before merge or release. Use for code reviews, failing checks, migration reviews and acceptance verification.
---

# Voqelis quality gate

The authoritative standards are [AGENTS.md](../../../AGENTS.md) and the current [CI workflow](../../../.github/workflows/ci.yml). This skill defines *what to verify and how to report evidence*, without restating project-wide rules.

## Review protocol

1. **Freeze the object under review.** Record PR number, exact head/base SHAs, changed files and the current CI run ID. If the branch moves, restart evidence collection for the new SHA.
2. **Build a requirement-to-proof map.** For each acceptance criterion cite the implementation location, its test (or manual scenario) and outcome. Treat omitted criteria as gaps even when the test suite is green. Read the relevant RFC, not just the PR description.
3. **Inspect risk edges relevant to the diff:** unauthorized or revoked user, foreign record, stale UI revision/callback, duplicate event, concurrent sender, restart, timezone/DST, migration from a real previous schema, privacy and resource limits. Mark not applicable where warranted.
4. **Validate execution separately.** If a working checkout is available, run the repository CI commands there and preserve the actual exit statuses. Independently inspect GitHub Actions jobs for the exact head SHA. A tool invocation, generated test or green badge alone is not evidence that *all* checks completed.
5. **Triage findings.** Report blocker / important / follow-up with file and affected behavior; distinguish proved failures from hypotheses. Fix only under an authorized development scope, and require fresh checks after edits.
6. **Return a gate summary.** Show the checked SHA, tests (PASS / FAIL / NOT RUN), requirement gaps, migration rehearsal status, manual Telegram acceptance still required, and whether the PR is ready for *human-authorized* merge.

## Release-sensitive reminder

A PR's green CI does not validate the live Telegram bot, production data, back-up restoration or a successfully deployed SHA. Use `voqelis-release` for those separate gates.
