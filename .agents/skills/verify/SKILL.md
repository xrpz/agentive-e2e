---
name: verify
description: Run the project's checks (lint, typecheck, test and the other verbs) through .agents/bin/verb and show pass/fail evidence. Use right before every commit and before saying any code change is done, fixed or passing.
disable-model-invocation: false
---

# /verify

Run every command below from the repository root: script and `.agents/bin/` paths are repo-root relative (never `cd` into the skill directory).

1. `.agents/bin/agentive-lint lock`. A mismatch means a skill changed outside its lock: do not commit; show the names to the human.
2. `.agents/bin/verb verify`. It runs every inner verb, writes `.agents/state/verify.json` and prints a short excerpt per verb.
   - Committing only part of the work tree (other edits unstaged or files untracked): run `.agents/bin/verb verify --staged` instead. It sets the unstaged and untracked changes aside, verifies exactly the staged content and restores them, also on failure; the record then matches the commit.
   - A last line `AGENTIVE_FAIL verb=<v> exit=<n> log=<path>; NEXT=<cmd>`: run that NEXT read command, fix the cause in the code, then rerun step 2. Never weaken, skip or delete a test, and never lower a threshold to pass.
   - `pending` verbs have no stack evidence yet: they are not failures; say which are pending. When every verb is pending, say verification is pending (scaffold the project, then /agentive:update); never call it done.
3. A bug fix shows red then green: before fixing, run the test that reproduces the bug and show it failing; after the fix, show it passing. Keep both for the PR's `## Verification`.
4. Report the commands you ran and their result lines. Claim done only when the verbs with a command pass.
