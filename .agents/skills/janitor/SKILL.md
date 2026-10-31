---
name: janitor
description: Small automated cleanup - for each kind a human listed in .agents/manifest.yaml janitor.kinds, fix at most batch_max items found by its deterministic sensor and open one PR labeled agentive-janitor:<kind>. Off by default (no kinds). Use in a headless or Routines run set up for janitor work, or when a human asks for a janitor pass.
disable-model-invocation: false
---

# /janitor

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

1. Read `janitor.kinds` in `.agents/manifest.yaml`. Empty or absent: print `janitor: no kinds configured (off by default); NEXT=none` and stop. Kinds, batch_max and frequency are widened only by a human editing that list.
2. For each kind `{id, sensor, batch_max}`:
   1. `sh .agents/skills/janitor/scripts/gate.sh <id>`. `SKIP <id>: #N open` (exit 1): make that your final line for this kind and go to the next kind. Exit 2: report its NEXT and stop.
   2. Run the sensor command exactly as written (through `.agents/bin/verb` when it is a verb). Take at most batch_max items from its output; nothing to fix -> next kind.
   3. Branch `janitor/<id>-<YYYYMMDD>` from the default branch; fix only those items, one kind per PR; run /verify; commit through the normal commit procedure.
   4. Open the PR with /pr, then label it at once: `gh api -X POST repos/{owner}/{repo}/issues/<n>/labels -f "labels[]=agentive-janitor:<id>"`, and check that the label is on the PR: `gh api repos/{owner}/{repo}/issues/<n>/labels --jq '.[].name'` must list `agentive-janitor:<id>` (retry the POST once if it does not). The CI review reads labels from the API when it runs, and a `labeled` event with this label re-runs the gate, so a label added a moment after the PR still makes it tier 2. The token that adds the label must not be the workflow's `GITHUB_TOKEN` (events made with it start no run); a Routines run uses the user's own token and `claude -p` in CI uses a GitHub App token. The PR is always tier 2: a human reads and merges it.
3. Report one line per kind: `PR #<n>`, `SKIP ...` or `nothing to fix`.

Run headless with the agentive standard command, prompt `/janitor`: `AGENTIVE_HEADLESS=1 CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1 claude -p "<prompt>" --permission-mode dontAsk --allowedTools "Read,Edit,Write,Glob,Grep,Bash(.agents/bin/verb *),Bash(.agents/bin/agentive-lint *),Bash(.agents/bin/agentive-review *),Bash(git add *),Bash(git commit *),Bash(git push *),Bash(gh api *),Bash(sh .agents/skills/*),Bash(git switch *),Bash(git fetch *)" --max-budget-usd <n> --output-format json`; in a Routines run the routine's cloud environment sets `AGENTIVE_HEADLESS=1`. Never merge, never push to the default branch, and never touch hooks, permissions, CI or review config in a janitor PR.
