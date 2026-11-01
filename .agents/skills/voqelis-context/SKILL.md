---
name: voqelis-context
description: Reconstruct the current, evidence-backed Voqelis state before continuing work, prioritizing GitHub commits, PRs, VPS production and unfinished requirements. Use for status, handoffs, resumed development and release planning.
---

# Voqelis context checkpoint

Use the repository-root [AGENTS.md](../../../AGENTS.md) as the authoritative project policy. This skill adds a *retrieval and reconciliation workflow*, not a second set of engineering rules.

## Workflow

1. **Anchor the request.** Identify the module, user-approved scope and the last confirmed checkpoint. Search the related code, tests and relevant RFCs; do not assume the repository README or an old conversation summary reflects the current implementation.
2. **Resolve GitHub state.** Query `0w1Max/voqelis` for the actual `feature/planner-v1` HEAD, affected PR state, PR head/base SHA and current CI conclusion. Distinguish merged code from unmerged Draft PRs. If a ref changed during the check, re-read it before proceeding.
3. **Resolve production independently.** Use actual VPS output when available: on-disk Git HEAD, `voqelis.service` health and running code evidence. Otherwise explicitly label the last user-provided successful deploy as *last verified*, with its date; never equate GitHub HEAD with a deployment.
4. **Reconcile requirements.** Compare user acceptance criteria, RFCs, affected source, tests and PR evidence. Mark implemented-but-not-deployed, partially implemented and planned items separately. Flag outdated documentation rather than silently treating it as current.
5. **Produce a handoff checkpoint**, with these fields:
   - GitHub integration SHA + evidence source;
   - deployed SHA/health + verification time, or **unknown**;
   - relevant PRs and CI states;
   - confirmed functionality and remaining acceptance checks;
   - risks/dependencies and the **next approval-gated action**.

## Guardrails

- The checkpoint is read-only: no pushes, merges, service actions or database mutations.
- Never commit a guessed or automatically refreshed production SHA to a static document; runtime status requires new evidence.
- Do not publish private Telegram content, server secrets or personal records in public PRs.
- If access to GitHub, VPS or logs is missing, use **not verified**, not **PASS**.
