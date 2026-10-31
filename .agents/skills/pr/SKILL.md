---
name: pr
description: Push the current work branch and open its PR, or refresh an open PR, with the agentive PR body (## Explainer first on tier 2, ## Verification, ## Risk, ## How to revert, the agentive-review block). Use after committing work, for every chain intent, spec and feature PR, and with --explain to write only ## Explainer.
argument-hint: "[--explain]"
disable-model-invocation: false
---

# /pr [--explain]

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

Arguments: `$ARGUMENTS` (Codex: read `--explain` from the user's message). All GitHub calls go through `gh api`; `gh pr` is blocked in cloud sessions and Routines runs.

## Open or refresh (no --explain)

1. Work on a work branch, never the default branch. Commit everything first (normal commit procedure); the PR shows committed work only.
2. `git push -u origin HEAD` (a work-branch push; never force, never the default branch).
3. `sh .agents/skills/pr/scripts/pr-body.sh draft --tool <claude_code|codex> --model <the model you run as>`. Add `--intent-skipped` when the user told you to skip intent.md on a hard-to-reverse change; add `--hotfix-of <n>` when this PR fixes the merged PR #n.
   - `AGENTIVE_PR=stop reason=verify_...`: do its NEXT (`.agents/bin/verb verify --staged` verifies exactly the committed tree), then draft again.
   - `AGENTIVE_PR=stop reason=base_ref_missing | no_default_branch | no_merge_base | no_commits_ahead`: the base `origin/<default>` could not be compared; do its NEXT, then draft again. Lines starting `NOTE` say what the script fetched or assumed.
   - `CHAIN_PROBLEM` lines: CI will block the PR (reason chain_order); fix the branch (one slug, one new artifact per PR) before publishing.
   - `MODE=refresh` (the branch already has an open PR): the draft holds only `## Verification`; fill it and go to step 5.
4. Edit the draft at its `PR_BODY=` path. Replace every `<!-- agent: ... -->` and `%% agent:` marker with real text or delete the line:
   - `## Explainer` (present when `EXPLAINER=required`: tier 2 predicted or uncertain): 3-6 lines in the PR's language (what changes, why, what the human must judge), every claim citing code as `path:line`, `path:start-end` or `path#symbol`; `### Before` = one mermaid flowchart of the current flow on the base branch for the part the PR touches, nodes labeled with code refs; `### After` = the same flow after the PR, nodes marked `:::added`, `:::changed`, `:::removed`, removed calls as dashed links `-.->`; keep the legend line. When the flow does not change, `### After` names the unaffected nodes and why. On a chain intent or spec PR, Before is the current behavior and After the behavior the artifact proposes.
   - `## Verification`: keep the generated lines; fill each R<n> result (pass | fail | not run); for a bug fix add the red run (fails before the fix) and the green run (passes after). No raw logs.
   - `## Risk`: what could break and how it was checked. Keep the line `intent skipped: user instruction` when it is there.
   - `## How to revert`: add steps beyond reverting the merge commit (data, config, flags).
   - Leave the `agentive-review` block as generated.
5. `sh .agents/skills/pr/scripts/pr-body.sh check <file> --explainer <required|optional as draft printed>`; fix until it prints `check_ok`.
6. `sh .agents/skills/pr/scripts/pr-body.sh publish <file> --title "<type>: <what, in a few words>"`.
   - No open PR: it opens one against the default branch.
   - An open PR: it replaces only `## Verification` (comments, checks and the rest of the body stay as they are). Then stop and report its last line `next: /pr-feedback <n>`.

## --explain

1. `sh .agents/skills/pr/scripts/pr-body.sh draft --explain`, fill the markers as in step 4, `sh .agents/skills/pr/scripts/pr-body.sh check <file> --explain-only`.
2. `sh .agents/skills/pr/scripts/pr-body.sh publish <file>`: replaces only `## Explainer` of the open PR (adds it first when missing). Run it again after a push that changes the explained flow.

Never merge, approve or enable auto-merge, never handle review comments or failing checks here (that is /pr-feedback), and never copy the agentive-review block from another PR.
