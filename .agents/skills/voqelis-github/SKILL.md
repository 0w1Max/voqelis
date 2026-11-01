---
name: voqelis-github
description: Manage Voqelis GitHub branches, Draft pull requests, CI failures, PR review and explicitly approved merges with exact-SHA verification. Use for repository and PR operations; never imply merge authorizes production deployment.
---

# Voqelis GitHub workflow

Read [AGENTS.md](../../../AGENTS.md) for the overall approval and security requirements and [CI](../../../.github/workflows/ci.yml) for the current test jobs. This skill covers GitHub sequencing, not production.

## Inspect

1. Identify the operation and exact user approval scope: read-only review, create branch, edit Draft PR, mark Ready, merge, or another explicit operation. A request to create a PR **does not** authorize a merge.
2. Read the current `feature/planner-v1` SHA and the relevant PR's base/head SHA and state. The default `main` branch is not the integration target.
3. Check dependencies on other PRs and potential branch drift. For stacked PRs, merge the approved prerequisite first, then re-evaluate diff, mergeability and CI of the dependent PR.
4. Retrieve actual CI jobs linked to the exact reviewed head. If CI ran on a different SHA, treat the new head as unverified.

## Write or merge

1. For approved code changes, create a dedicated branch from a verified SHA, then make the minimum diff.
2. Publish an honest Draft PR containing scope, changed files, tests run or not run, behavior risks, dependencies, database migration notes, manual acceptance and release gates.
3. Resolve CI failures with a new commit, then verify the newly triggered run. Never reuse an earlier green status as proof for modified code.
4. **Only after explicit approval** mark Ready, then merge with an expected-head-SHA guard. If the head or target changed unexpectedly, stop and report rather than merging a different change.
5. After a merge, fetch PR metadata and compare the merge SHA against the integration branch HEAD. Report both identities and distinguish them from the last verified VPS production HEAD.

## Output

Report action performed, PR URL, head/base/merge SHAs, CI status, touched scope, approval still needed and production state. Do not expose private logs, tokens, `.env` or user content.

Do not invoke this skill as permission to deploy. Use `voqelis-release` only after separately authorized production rollout.
