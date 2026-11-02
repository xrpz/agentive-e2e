---
name: precommit-review
description: Fresh-context review of the staged change before an agent commit (deterministic checks, trust boundary, one correctness reviewer subagent); returns the blocking findings and whether the human must confirm. Use after /verify and before every commit you make.
disable-model-invocation: false
---

# /precommit-review

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

You orchestrate; you never review the change yourself. The review is done by a reviewer subagent that you start in a fresh context: it reads only its input file (the staged diff, the review criteria and the files the diff touches), never your conversation. Claude Code and Codex alike: start the reviewer from this conversation, one level deep, never from inside another subagent.

1. `git diff --cached --stat`. Nothing staged: return `PRECOMMIT=none; NEXT=stage the change first`.
2. `.agents/bin/agentive-review precommit --tool <claude|codex>` (the tool you run in). It runs the precommit profile (RV1 checks from `.agents/state/verify.json`, RV2 trust boundary and confirm scope, RV5 correctness-reviewer against code_review.md) and writes `.agents/state/precommit-review.json`.
   - Exit 3 with `AGENTIVE_REVIEW_TASK id=.. agent=.. input=.. output=.. meta=..` lines: for each task start the named subagent (`.claude/agents/<agent>.md`, Codex `.codex/agents/<agent>.toml`) in the foreground (Claude Code Agent tool with `run_in_background: false`), with exactly this prompt and nothing else: `Read <input> and do the task it describes. Reply with the JSON object only.` Add no context, intent, summary or argument of yours. Write its JSON reply verbatim to the output path, write `{"executed_by": "subagent"}` to the meta path, then run the same command again.
   - Only when you have no tool to start a subagent, or starting it failed: do the task yourself following `.agents/agents/<agent>.md`, reading only its input file, and write `{"executed_by": "self", "reason": "no_agent_tool"}` (or `"spawn_failed"`) to the meta path. Do not stop: the record says `self` and the command prints one `NOTE independence=self` line.
   - The record is honest and advisory: `isolated` only when a SubagentStart or SubagentStop hook saw the subagent; a task you did yourself is recorded as `self` / `shared` with its reason; nothing observed is `unknown` / `unverified` (Codex local: always so; for a structurally separate review run `codex exec --sandbox read-only` yourself). Independence is enforced in CI, attested here; do not claim more than the record says.
3. Return exactly:
   - the `NOTE` lines it printed and the command's last line (`AGENTIVE_PRECOMMIT=commit | blocked | confirm ...; NEXT=...`), unchanged;
   - one line per blocking finding from `.agents/state/precommit-review.json`: `BLOCKING <file>:<line> <trigger> - <summary>`;
   - `nits=<n>` (count only; nits are never fix requests).
4. The NEXT decides the commit: blocked -> fix and rerun, do not commit; confirm with a human present -> ask the human and, on yes, run `.agents/bin/agentive-review precommit --human-confirmation obtained` (it reuses this review) before committing; confirm with no human present (the NEXT names it after `; if no human is present to answer`) -> never commit to the default branch; on a work branch commit there and open a PR with /pr (tier 2, a human merges it), on the default branch or a detached HEAD create a work branch first. Trust-boundary hits do not block the commit; a human approves them at the PR.
