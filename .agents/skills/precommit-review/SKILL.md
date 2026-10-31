---
name: precommit-review
description: Fresh-context review of the staged change before an agent commit (deterministic checks, trust boundary, one correctness reviewer); returns the blocking findings and whether the human must confirm. Use after /verify and before every commit you make.
context: fork
background: false
disable-model-invocation: false
---

# /precommit-review

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

You review a change you did not write: use only the staged diff, the review criteria and the files the diff touches, never the implementer's conversation. Codex: run these steps in a subagent so the review does not see the implementer's conversation.

1. `git diff --cached --stat`. Nothing staged: return `PRECOMMIT=none; NEXT=stage the change first`.
2. `.agents/bin/agentive-review precommit --tool <claude_code|codex>` (the tool you run in). It runs the precommit profile (RV1 checks from `.agents/state/verify.json`, RV2 trust boundary and confirm scope, RV5 correctness-reviewer against code_review.md) and writes `.agents/state/precommit-review.json`.
   - Exit 3 with `AGENTIVE_REVIEW_TASK id=.. agent=.. input=.. output=.. meta=..` lines: run each task with the named subagent in a fresh context (`.claude/agents/<agent>.md`, Codex `.codex/agents/<agent>.toml`); where you cannot start one, do the task yourself following `.agents/agents/<agent>.md`, reading only its input file. Write the JSON reply verbatim to its output path, write `{"executed_by": "subagent"}` to its meta path (`{"executed_by": "self"}` when you did the task yourself), then run the same command again.
   - The record is honest and advisory: `isolated` only when a SubagentStart or SubagentStop hook saw the subagent; a task you did yourself is recorded as `self` / `shared`; nothing observed is `unknown` / `unverified` (Codex local: always so; for a structurally separate review run `codex exec --sandbox read-only` yourself). Independence is enforced in CI, attested here; do not claim more than the record says.
3. Return exactly:
   - the command's last line (`AGENTIVE_PRECOMMIT=commit | blocked | confirm ...; NEXT=...`), unchanged;
   - one line per blocking finding from `.agents/state/precommit-review.json`: `BLOCKING <file>:<line> <trigger> - <summary>`;
   - `nits=<n>` (count only; nits are never fix requests).
4. The NEXT decides the commit: blocked -> fix and rerun, do not commit; confirm with a human present -> the implementer asks the human and, on yes, runs `.agents/bin/agentive-review precommit --human-confirmation obtained` (it reuses this review) before committing; confirm with no human present -> no commit to the default branch, only a work branch and /pr (tier 2). Trust-boundary hits do not block the commit; a human approves them at the PR.
