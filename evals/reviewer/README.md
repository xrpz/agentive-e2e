# Reviewer eval cases

One case per file: `evals/reviewer/<id>.yaml` (schema reviewer-eval-case; the id equals the file name). The set is the
only eval agentive keeps. CI job `reviewer-eval` runs the whole set only on a PR that changes the reviewer (a prompt in
`.agents/agents/`, `.agents/bin/agentive-review`, `code_review.md`, `REVIEW.md`, the ai-review job, or
`review.pipeline` in `.agents/framework.yaml`): 3 runs with the base branch's reviewer config and 3 with the PR's,
then one PR comment with the before/after result. There is no scheduled run.

Cases are review config (protected_check_config): a PR that adds or changes one is tier 2, and the human merge of that
PR is the confirmation of the bug location. Agents draft cases (/distill does it for reverts, hotfixes and review
noise); humans merge them.

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
- A counted finding catches a range when its file is equal and its line, or one of its `instances[].line`, is inside
  `[line_from, line_to]`.
- Positive passes: every expected range is caught. Negative passes: no blocking finding and no counted finding in a
  forbidden range. A case's result is its majority over the 3 runs.
- The change is suggested for adoption when no positive is newly missed and either the score improves by more than
  the baseline's run-to-run spread or no case regresses. Otherwise merge only with the PR-body line
  `eval-override: <reason>`. With fewer than one positive and one negative case the comment says the change is not
  verified.
