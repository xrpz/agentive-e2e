---
name: chain-spec
description: Second chain stage - write docs/plans/<slug>/spec.md (numbered requirements R<n> each with one verification means, design, how each intent constraint is met) after the intent PR is merged, then open the spec PR. Use for /chain-spec <slug> or when a merged intent.md needs its spec.
argument-hint: <slug>
disable-model-invocation: false
---

# /chain-spec <slug>

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

Slug: `$0`. If it reads literally `$0` or is empty (Codex), take it from the user's message.

1. Run `sh .agents/skills/chain-spec/scripts/stage-check.sh <slug>` alone in its Bash call (no `; echo $?`: the script prints its result). If it prints an `AGENTIVE_CHAIN=stop:...` line, print that line, stop, and tell the human its NEXT.
2. `git switch -c spec/<slug> --no-track origin/<DEFAULT>` (no upstream until /pr runs `git push -u origin HEAD`). Read `docs/plans/<slug>/intent.md`, AGENTS.md and the code the change touches.
3. Write `docs/plans/<slug>/spec.md` from `templates/spec.md`:
   - Requirements R1, R2, ... each with exactly one verification means (a test name or a command). Never renumber or reuse a number; a dropped requirement keeps its line marked `(dropped)`.
   - Map every constraint of the intent to an R<n> or a design item.
   - Answer every open question of the intent, or carry it forward to `## Areas of concern`.
   - A conflict between normative sources (rules, ADRs, review criteria, policies) is never resolved silently: list it under `## Areas of concern` with the owner who decides it.
   - Missing information: look it up first (code, docs, git history, tracker and CI through a CLI, connected tools). A human is present: ask. No human present and the question is hard to reverse (data deletion, schema, payments, mass sends): stop, leave the question in spec.md `## Open questions from the intent`, and write a handoff: `sh .agents/skills/chain-spec/scripts/handoff.sh "<the question>" "<what the human must decide>"` (it prints `HANDOFF=<path>`). Otherwise assume, and add a `[inferred]` line under `## Decisions`.
4. Edit no code and no file other than spec.md. Commit only spec.md on `spec/<slug>`.
5. Run /pr (Codex: $pr) to push and open the spec PR. It is always tier 2: a human merge accepts the spec, closing rejects it. The human checks first that the spec solves the intent's problem and that every open question is answered or carried forward.

Never push to the default branch, merge, or write plan.md here.
