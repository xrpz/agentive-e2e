# Reviewer eval cases

One case per file: `evals/reviewer/<id>.yaml` (schema reviewer-eval-case; the id equals the file name). The set is the
only eval agentive keeps. CI job `reviewer-eval` runs the whole set only on a PR that changes the reviewer (a prompt in
`.agents/agents/`, `.agents/bin/agentive-review`, `code_review.md`, `REVIEW.md`, the ai-review job, or
`review.pipeline` in `.agents/framework.yaml`): 3 runs with the base branch's reviewer config and 3 with the PR's,
then one PR comment with the before/after result. There is no scheduled run.

Cases are review config (protected_check_config): a PR that adds or changes one is tier 2, and the human merge of that
PR is the confirmation of the bug location. Agents draft cases (/distill does it for review escapes and review noise
when a location exists, with `sh .agents/skills/distill/scripts/eval-case.sh`); humans merge them. A revert alone changes
the whole PR and names no line, so /distill writes no case for it and says so in its PR body.

## Positive case: a PR that let a bug through

```yaml
id: login-null-session          # = file name
pr: "#412"                      # the PR the reviewer runs on: #N, or <base sha>..<head sha>
label: positive
origin: {kind: revert, ref: "#431"}   # fix_pr | revert | reproduction_diff | injected_bug (injected only with the human check)
expected_findings:               # the ground-truth bug location(s); every range must be caught
  - {file: src/auth/session.py, line_from: 40, line_to: 44}
budget_usd: 2                    # optional --max-budget-usd of each headless run
```

Optional fields (all of them; a case without them stays valid):

```yaml
base_sha: 0123456789abcdef0123456789abcdef01234567   # with head_sha: the commits the line numbers refer to; the runner uses
head_sha: 89abcdef0123456789abcdef0123456789abcdef   #   them instead of asking the PR again (a force-push or squash cannot move the lines)
expected_findings:
  - {file: src/auth/session.py, line_from: 40, line_to: 44, slack: 0}   # slack: this range only; 0..20, default 2
bug_class: error-handling        # logic | error-handling | concurrency | data-contract | security | test-gap | spec-drift | other
note: "session may be None"      # one human-readable line; never scored
source: {finding_id: F-1a2b3c4d5e, reply: wrong, range_basis: proposed}   # where a drafted case came from; range_basis: proposed | confirmed
created: 2026-11-02
drafted_by: distill              # distill | human
```

`expected_findings` lines are the lines of `head_sha` (the new side of the PR). Keep a range narrow (one bug, one range, at most
about 20 lines): a range that covers the whole PR makes any finding "right". A positive case with several ranges needs every one
of them caught.

## Negative case: a known-clean PR, or a PR whose findings were answered wrong or intentional

```yaml
id: retry-backoff-clean
pr: "#398"
label: negative
forbidden_findings:              # optional: where a finding must not appear again
  - {file: src/net/retry.py, line_from: 10, line_to: 18}
```

## Scoring (what the CI comment reports)
- Counted findings are the ones the pipeline would post: severity important, on a line, not refuted by the verifier.
- A counted finding catches an expected range when its file is equal and its line, or one of its `instances[].line`, is inside
  `[line_from - slack, line_to + slack]`. `slack` is the range's own value, else 2 lines (a default chosen to absorb a
  finding anchored on the function line instead of the body; no official source gives a value). A forbidden range of a
  negative case has slack 0.
- Diagnostic, not scored: a positive run that missed the range but posted a counted finding on the same file at another line
  (with the same `bug_class` when the case has one) is shown as "wrong place". The code cannot tell the same bug on the wrong
  line from another problem, so it never changes pass, `newly_missed` or the adopt suggestion; a human reads it and widens the
  range when the finding was right.
- Positive passes: every expected range is caught. Negative passes: no blocking finding and no counted finding in a
  forbidden range. A case's result is its majority over the 3 runs.
- The change is suggested for adoption when no positive is newly missed and either the score improves by more than
  the baseline's run-to-run spread or no case regresses. Otherwise merge only with the PR-body line
  `eval-override: <reason>`: a rule for the human who merges; no check reads the line and the gate does not wait for it,
  the line records why the change was adopted. With fewer than one positive and one negative case the comment is headed
  "not scored — review this reviewer change by hand": its Result line says why nothing was compared, its What to do line
  tells the human to review the change by hand and merge with that line, and it says to add the missing cases; the PR
  summary comment (`agentive-review-summary`) repeats it as one `**Reviewer eval:**` line.
- The comment opens with the verdict and what to do, then the score table, one sentence on run-to-run noise and the
  changed-case table (case, expected location, before k/N, after k/N, the change in words; regressions first). With fewer than
  10 positive cases it adds "Reference only: fewer than 10 positive cases": one case can move the totals, so read the changed
  cases. Folded under `<details>`: the runs of the changed cases (caught, missed with the finding that was posted, clean,
  noisy), the full per-case table, the changed reviewer files and the machine-read `yaml agentive-reviewer-eval` block. The
  block may carry `wrong_place` (the diagnostic above); summaries without it stay valid.
