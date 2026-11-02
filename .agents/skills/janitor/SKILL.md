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
   3. Create the branch with `git switch -c janitor/<id>-<YYYYMMDD> --no-track origin/<default branch>` (the Branches line of AGENTS.md); fix only those items, one kind per PR; run /verify; commit through the normal commit procedure.
   4. Open the PR with /pr and pass the label to it: `--label agentive-janitor:<id>` on the `publish` command of /pr step 6 (add `--title "chore(janitor): <id> ..."`). Publish adds the label, checks that the PR carries it (one retry) and prints `LABELED agentive-janitor:<id> pr=<n>` before its final line, so nothing is left to do after /pr: in a headless run /pr ends with `AGENTIVE_PR=done ... labels=agentive-janitor:<id>` and the run ends there. A last line `AGENTIVE_FAIL tool=pr-label`: follow its NEXT (`sh .agents/skills/janitor/scripts/label-pr.sh <id> <pr number>` repairs the label) and say in the report that the PR is not labeled. The label makes the PR tier 2: a human reads and merges it. The token must not be the workflow's `GITHUB_TOKEN` (events made with it start no run, so the gate would not re-run on the label).
3. Report one line per kind: `PR #<n>`, `SKIP ...` or `nothing to fix`.

Run headless with the agentive standard command, prompt `/janitor`: `AGENTIVE_HEADLESS=1 CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1 claude -p "<prompt>" --permission-mode dontAsk --allowedTools "Read,Edit,Write,Glob,Grep,Bash(.agents/bin/verb *),Bash(.agents/bin/agentive-lint *),Bash(.agents/bin/agentive-review *),Bash(git add *),Bash(git commit *),Bash(git push *),Bash(gh api *),Bash(sh .agents/skills/*),Bash(git switch *),Bash(git fetch *)" --settings '{"sandbox":{"autoAllowBashIfSandboxed":false}}' --max-budget-usd <n> --output-format json`; in a Routines run the routine's cloud environment sets `AGENTIVE_HEADLESS=1`. Never merge, never push to the default branch, and never touch hooks, permissions, CI or review config in a janitor PR.
