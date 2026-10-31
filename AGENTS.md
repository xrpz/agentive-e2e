<!-- AGENTIVE START context -->
<!-- Managed by agentive; edits inside this region are reported by /agentive:update and replaced only with --force or when you accept them. Keep text you want preserved outside the markers. -->
## Project (generated)

Stack (from evidence): Python (uv) (pyproject.toml, uv.lock).
Tests: tests.
Docs (pointers to files that exist): README.md, docs/adr/. Read the one that matches your task before guessing.

Run every verb through `.agents/bin/verb <verb>`: it keeps full output in `.agents/state/logs/` and prints a short excerpt; on failure follow the `NEXT=` of its last line.
- `setup`: `.agents/bin/verb setup` runs `uv sync --frozen` (evidence: uv.lock)
- `build`: `.agents/bin/verb build` runs `uv build` (evidence: pyproject.toml [build-system])
- `lint`: `.agents/bin/verb lint` runs `uv run ruff check --extend-exclude .agents,.claude,.codex .` (evidence: ruff configuration)
- `typecheck`: pending (no evidence)
- `test`: `.agents/bin/verb test` runs `uv run pytest` (evidence: pyproject.toml (pytest))
- `verify`: `.agents/bin/verb verify` runs lint, test (the inner verbs)
- `run`: pending (no evidence)

Review rules: @code_review.md
Codex: a `/<skill>` in this file and in skill text is `$<skill>` (for example `$verify`, `$pr`); `@code_review.md` is a Claude Code import, so open code_review.md yourself.
Chain entry (intent.md needed?): yes if (a) the diff cannot be described in one sentence, (b) it touches two or more modules, (c) the approach is uncertain, or (d) it is hard to reverse; yes -> say so in one line and run /chain-intent <slug> (intent -> spec -> plan); no -> implement directly; a user instruction overrides both ways.
Chain tasks: before working on docs/plans/<slug>/, read intent.md, spec.md and plan.md there. Stages: /chain-intent, /chain-spec, /chain-plan <slug> (Codex: $chain-intent ...); /pr pushes and opens every PR.
Commit: run /verify, then /precommit-review; commit only when it reports no blocking finding. A staged change to hooks, permissions, CI, .claude/settings.json, .codex/ or review config needs the human's yes first; with no human present, commit on a work branch and open a PR with /pr instead.
Session start: follow the `NEXT=` of each line it prints (HANDOFF, PENDING_CONFIRMATION, DIRTY, HARVEST); a HANDOFF for this branch is answered by the human (resume / discard / later) before any other work.
Harvest: when SessionStart prints `HARVEST=<n>; NEXT=ask_user_to_run:/distill`, offer /distill (Codex: $distill) once at the end of the task and run it only after the human agrees; never start it on your own.
Docs vs code: descriptive docs (paths, commands, counts, behavior) follow the code, so fix them in the same change; when code violates a rule, ADR or spec, change neither side and tell a human.
Running the app: use the project's run skill; if none exists, generate one with Claude Code's /run-skill-generator.
Worktrees: untracked files a new Claude Code worktree needs (for example .env) are listed in .worktreeinclude.
<!-- AGENTIVE END context -->

<!-- AGENTIVE START path-rules -->
<!-- Managed by agentive; edits inside this region are reported by /agentive:update and replaced only with --force or when you accept them. Keep text you want preserved outside the markers. -->
## Path rules (generated)

No rules yet.
<!-- AGENTIVE END path-rules -->
