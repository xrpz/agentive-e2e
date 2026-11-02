---
name: pr
description: Push the current work branch and open its PR, or refresh an open PR, with the agentive PR body (## Explainer first on tier 2, ## Verification, ## Risk, ## How to revert, the folded agentive review record). Use after committing work, for every chain intent, spec and feature PR, and with --explain to write only ## Explainer.
argument-hint: "[--explain]"
disable-model-invocation: false
---

# /pr [--explain]

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

Arguments: `$ARGUMENTS` (Codex: read `--explain` from the user's message). All GitHub calls go through `gh api`; `gh pr` is blocked in cloud sessions and Routines runs.

## Open or refresh (no --explain)

1. Work on a work branch, never the default branch. Commit everything first (normal commit procedure); the PR shows committed work only.
2. `git push -u origin HEAD` (a work-branch push; never force, never the default branch).
3. `sh .agents/skills/pr/scripts/pr-body.sh draft --tool <claude|codex> --model <the model you run as>`. Add `--intent-skipped` when the user told you to skip intent.md on a hard-to-reverse change; add `--hotfix-of <n>` when this PR fixes the merged PR #n; add `--request "<issue URL, or the user's request in one sentence>"` when you know where the change was asked for (a chain task gets its `docs/plans/<slug>/` line by itself).
   - `AGENTIVE_PR=stop reason=...` (verify, or a base `origin/<default>` that could not be compared): the script wrote nothing; do its NEXT, then draft again. Lines starting `NOTE` say what the script fetched or assumed.
   - `CHAIN_PROBLEM` lines: CI will block the PR (reason chain_order); fix the branch (one slug, one new artifact per PR) before publishing.
   - `NOTE PRECOMMIT_STALE ...`: the saved precommit review is of other content, so the draft carries no review entry from it; follow the note's NEXT (run /precommit-review on this content) or publish without one: CI reviews the PR either way.
   - `MODE=refresh` (the branch already has an open PR made by /pr): the draft holds only `## Verification`; fill it and go to step 5.
   - `MODE=adopt` (the open PR has none of the /pr sections, for example one opened on the GitHub web UI): the draft is the whole body (tier 2 starts with `## Explainer`); fill it as in step 4.
4. Edit the draft at its `PR_BODY=` path. Replace every `<!-- agent: ... -->` and `%% agent:` marker with real text or delete the line:
   - `## Explainer` (present when `EXPLAINER=required`: tier 2 predicted or uncertain), for a human who must see the changed workflow at a glance:
     - First line `**What changes:** <one sentence>`, then 3-5 bullets in the PR's language: new or changed, unchanged, what the human must judge; every claim cites code as `path:line`, `path:start-end` or `path#symbol`. Code locations go in these bullets, never in the diagram.
     - `### Before`: one mermaid `flowchart TD` of the current flow on the base branch for the part the PR touches; `### After`: the same flow after the PR. 3-10 nodes and at most 12 arrows each, one entry node, one node or one arrow per line. Entry and result `id(["..."])`, steps `id["..."]`; a label is a plain step starting with a verb (25 characters or fewer per line), then `<br/>` and the symbol name (`check_password()`); no paths, `#` or line numbers.
     - Same look in both: theme colors only (no `fill`, `color`, `style`, `linkStyle`, `%%{init}` or frontmatter). Before has no classDef. After keeps every Before node with the same id and label and marks the change with a text tag and the one accent border: `NEW<br/>...` + `:::added`, `CHANGED<br/>...` + `:::changed`, a deleted Before step `REMOVED<br/>...` + `:::removed`; arrows: unchanged `-->`, new `==>`, removed `-.->|removed|`; keep the three classDef lines of the draft and the `How to read:` line under After as they are.
     - A filled example that renders in light and dark themes: `.agents/skills/pr/templates/explainer-example.md`.
     - Draw only steps and calls that exist in that tree (Before: base, After: head); a new function nothing calls yet gets an arrow label saying so. When the flow does not change, `### After` names the unaffected nodes and why. On a chain intent or spec PR, Before is the current behavior and After the behavior the artifact proposes.
   - `## Verification`: keep the generated lines; fill each R<n> result (pass | fail | not run); for a bug fix add the red run (fails before the fix) and the green run (passes after). No raw logs.
   - `## Risk`: what could break and how it was checked. The first line is `Request: <issue URL | docs/plans/<slug>/ | user request: "<one sentence in the user's words>">`: where this change was asked for (reviewers check a new capability against it); replace its marker, never delete it. Keep the line `intent skipped: user instruction` when it is there.
   - `## How to revert`: add steps beyond reverting the merge commit (data, config, flags).
   - Leave the `## agentive review record` section (the folded `agentive-review` block) as generated.
5. `sh .agents/skills/pr/scripts/pr-body.sh check <file> --explainer <required|optional as draft printed>`; fix until it prints `check_ok`. `PROBLEM explainer_style:<code>: ...` lines are the Explainer rules above, checked here only (CI checks that the section, Before, After and their diagrams exist).
6. `sh .agents/skills/pr/scripts/pr-body.sh publish <file> --title "<type>: <what, in a few words>"`. Add `--label <name>` (repeatable) for each label the PR needs when the calling skill asks for one (the janitor passes `agentive-janitor:<kind>`): publish adds it through `gh api`, reads it back (one retry) and prints `LABELED <names> pr=<n>` before its final line, so no step after /pr is needed. A last line `AGENTIVE_FAIL tool=pr-label ...`: the PR exists but a label is missing; follow its NEXT and say in the report that the PR is not labeled.
   - No open PR: it opens one against the default branch.
   - An open PR made by /pr: it replaces only `## Verification` (comments, checks and the rest of the body stay as they are). Then stop and report its last line `next: /pr-feedback <n>`.
   - An open PR without the /pr sections (`MODE=adopt`): it writes the whole drafted body and keeps the old text, every line behind `> `, under `## Original description` (the old body is also saved to `.agents/state/pr-body-before-<n>.md`); then the lines below as usual.
   - `AGENTIVE_PR=stop reason=...` (the PR changed after the draft was made): nothing was written; do its NEXT (draft again, or publish with `--replace-body` to write the whole drafted body).
   - `MERGE=human pr=<n> url=<url>` (tier 2: the install PR, a workflow change, a trust-boundary path, a chain intent or spec PR): a human reviews and merges it; tell the human the URL and never merge it yourself. With no human present (headless runs (`claude -p`), Routines runs) the last line is `AGENTIVE_PR=done ... waiting=human_merge` (with `labels=<names>` when `--label` was given): report the PR URL and end the run.

## --explain

1. `sh .agents/skills/pr/scripts/pr-body.sh draft --explain`, fill the markers as in step 4, `sh .agents/skills/pr/scripts/pr-body.sh check <file> --explain-only`.
2. `sh .agents/skills/pr/scripts/pr-body.sh publish <file>`: replaces only `## Explainer` of the open PR (adds it first when missing). Run it again after a push that changes the explained flow.

Never merge, approve or enable auto-merge, never handle review comments or failing checks here (that is /pr-feedback), and never copy the agentive-review block from another PR.
