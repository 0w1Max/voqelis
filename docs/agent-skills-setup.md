# Voqelis Agent Skills — setup and use

This guide describes **repo-scoped Codex Agent Skills**, not a deployment component of the Telegram bot.

## What is included

| Skill | When to use |
| --- | --- |
| `voqelis-context` | Resume work, check source/production SHAs, PR states and next verified step |
| `voqelis-develop` | Implement an explicitly approved feature or fix with tests |
| `voqelis-quality` | Review a PR, failures, acceptance gaps, regressions and CI evidence |
| `voqelis-github` | Create/review Draft PRs and carry out explicitly authorized merges |
| `voqelis-release` | Plan or verify an expressly authorized VPS release |
| `voqelis-telegram-qa` | Prepare or review manual Telegram voice/text/button acceptance |

The canonical engineering policy is [`AGENTS.md`](../AGENTS.md); the canonical Bash standard is [`docs/terminal-script-standard.md`](terminal-script-standard.md). Skills intentionally reference these files rather than duplicating them.

## Step 1 — inspect changes on GitHub

1. Review the Agent Skills Draft PR and its "Files changed" tab.
2. Check that GitHub Actions reports success **for the latest PR head SHA**, including `Agent Skills`, Ruff, Compile, Pytest and smoke.
3. Until merge is separately approved, the skills exist **only on the PR branch**, not on `feature/planner-v1`.
4. A merged PR does not authorize any VPS deployment.

## Step 2 — open the skills in Codex (Windows 11, no shell required)

1. Install or open the current ChatGPT desktop app for Windows and sign in; select **Codex** if available for your account. Consult the official [quickstart](https://developers.openai.com/codex/quickstart).
2. If no local clone exists, use **GitHub Desktop → File → Clone repository** for `0w1Max/voqelis`. Choose a normal local folder such as `Documents/Projects/voqelis`; do not use `/opt/voqelis` or the VPS as a workspace.
3. In GitHub Desktop fetch from origin. While PR is Draft, switch to `docs/voqelis-agent-skills-v1` to preview it. After approved merge, switch back to `feature/planner-v1` and fetch/pull the updated integration branch.
4. In Codex, open the **repository root** as the local workspace. Codex scans the root `.agents/skills` directory for `SKILL.md` manifests; do not copy these six folders into your global user profile.
5. Use the Codex `/skills` selector, or type `$` followed by a skill name, for example `$voqelis-context`. If a newly added skill is missing, reopen/restart Codex and ensure that the active checked-out branch contains the files.
6. Start with **read-only** exploration before authorizing edits:

   `$voqelis-context Сверь GitHub HEAD, последний подтверждённый VPS HEAD и открытые PR. Не изменяй файлы и не выполняй deploy.`

7. For development, request the narrow task and use `$voqelis-develop`; for a review, `$voqelis-quality`. For Telegram manual acceptance, `$voqelis-telegram-qa`.

Official skill discovery instructions: [Build skills](https://developers.openai.com/codex/skills).

## Step 3 — verify skill manifests

From the repository root, run the standard-library Python validator:

`python tools/verify_agent_skills.py`

It checks the six expected skills, frontmatter name/description, uniqueness and relative Markdown reference links. This is **read-only** and does not connect to the VPS. The same command is included in the CI workflow. To run regression tests locally, follow [the repository CI workflow](../.github/workflows/ci.yml).

## Step 4 — using skills in ChatGPT

Codex and ChatGPT skill installation are **not identical**. These repo-local folders are automatically discoverable in supported Codex workspaces, but **do not automatically install into ordinary ChatGPT chats**.

- If your ChatGPT workspace exposes **Plugins → Skills**, follow the UI flow to create/upload an independently packaged skill, or use a supported plugin. Availability depends on the account and workspace.
- For a normal ChatGPT chat without skill-installation access, keep using the GitHub connection and link this repository; the instructions can still be read as documents, but this is not equivalent to installing a skill.

Official account-specific guide: [Skills in ChatGPT](https://help.openai.com/en/articles/20001066-skills-in-chatgpt).

## Operations and permissions

| Action | Requires specific user authorization |
| --- | --- |
| Read GitHub code/PRs and inspect CI | No extra mutation approval |
| Change repository files / open PR | Scope approval |
| Mark Ready / merge | Explicit PR approval |
| Update VPS code, restart service or migrate live SQLite | Separate explicit production approval |
| Send mass notifications to approved users | Explicit owner broadcast confirmation |

Do not place server secrets, `.env`, private Telegram recordings, IDs or user data in the public repository, a skill, or PR description.

## Troubleshooting

- **Skill not visible:** check the active branch, that Codex opened the repo root, and that the folder contains `SKILL.md`. Restart Codex if discovery is stale.
- **Invalid skill:** run `python tools/verify_agent_skills.py`, fix the reported name/description/link, then re-run CI.
- **Cannot run tests in a chat:** skill instructions do not grant a terminal; use a Codex workspace with a local checkout and Python dependencies, or rely on GitHub Actions and distinguish them.
- **Unexpected production SHA:** stop; gather fresh read-only production evidence. Never assume GitHub merge automatically deployed.
