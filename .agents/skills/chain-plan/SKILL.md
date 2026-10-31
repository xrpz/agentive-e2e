---
name: chain-plan
description: Third chain stage - plan in read-only plan mode with the human, then commit the approved docs/plans/<slug>/plan.md as the first commit of feature/<slug> and implement it slice by slice. Use for /chain-plan <slug> after the spec PR is merged.
argument-hint: <slug>
disable-model-invocation: false
---

# /chain-plan <slug>

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

Slug: `$0`. If it reads literally `$0` or is empty (Codex), take it from the user's message.

1. Run `sh .agents/skills/chain-plan/scripts/stage-check.sh <slug>`. On exit 1 print its `AGENTIVE_CHAIN=stop:...` line and stop (it also stops when no human is present: the plan needs a human's approval).
2. Planning happens in a read-only mode. Claude Code: plan mode (Shift+Tab, or `claude --permission-mode plan`); never plan in a session where bypass permissions are available, because plan mode's blocks are not enforced there. Codex: `/plan`. If you are not in it, ask the human to switch and wait.
3. Read intent.md, spec.md and the code. Write the plan from `templates/plan.md` so that someone who never saw this conversation, or the other tool in a fresh session, can implement from intent.md, spec.md and plan.md alone:
   - vertical slices in order of work, each verifiable on its own, each citing its requirements `(R1, R3)` or ending with `(no R: <reason>)`; never layer by layer (DB, then API, then UI);
   - `## Risks`: what could break, the riskiest step, and how each risk is verified;
   - `## Decisions`: each line tagged `[user]`, `[doc: <path|URL>]` or `[inferred]`, including options considered and not chosen;
   - `## Open questions`: questions found while planning.
   Iterate with the human until they approve the plan (Claude Code: the accept choice of the plan-mode prompt).
4. After approval: `git switch -c feature/<slug> origin/<DEFAULT>`, write `docs/plans/<slug>/plan.md`, and commit only it, before any code change.
5. Implement slice by slice: when a slice's verify passes, check its box and commit (normal commit procedure: /verify, /precommit-review). When the work departs from the plan, update plan.md in the same commit and log why under `## Decisions`.
6. Run /pr (Codex: $pr) for the feature PR.

Never push to the default branch, merge, or change intent.md or spec.md here (they change only through a new tier 2 PR).
