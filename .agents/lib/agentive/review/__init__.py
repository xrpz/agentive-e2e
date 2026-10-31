# agentive-unit: lib.agentive
# design: spec.review_pipeline, spec.repo_layer.files[f-reviewers] (owner D, docs/IMPLEMENTATION.md §7)
"""agentive review pipeline (RV1 -> RV2 -> RV3 -> RV4 -> RV7b -> RV5 -> RV7 -> RV6 -> RV8), python3 stdlib only.

Owner: D. Modules:
  util       globs (`**` semantics), fenced-block extraction, stable ids, time
  gitio      git plumbing (diff, numstat, show, history) through `git` only
  config     review-config, review-rules (from the base commit), rubric (preset + override), review.auto_merge (framework default low_risk), manifest
  rv1        deterministic preprocess: checks, exclusions, diff stats, criteria map, signals, chain check
  rv2        trust boundary, protected_check_config, always_human, confirm_scope + U2 record
  rv3        change classification: rules first, LLM layer raise-only (never a tier-0 class)
  rv4        tier, raise-only check validation
  rv7b       binary rubric score (agentive:rubric@2; @1 pinnable)
  findings   finding evidence filter, stable ids, agentive-reply lines, convergence
  pr_body    PR body: the ```yaml agentive-review``` block, all-tier headings, tier-2 ## Explainer presence
  independence  CI agentive-independence rules
  llmstate   ai-review LLM step state: error code and fixed sentence (E2E-10b), workflow-validation skip detection (E2E-11)
  settings   `agentive-lint settings --effective`: effective settings and why auto-merge is unavailable
  killswitch U1.c kill switch (pure function over PR and git history)
  rv8        policy gate (pure function) -> block | human_required | pass (+ auto_merge)
  tasks      LLM task protocol: the script writes task inputs, the agent runs the named subagent in a fresh
             context and writes the subagent's JSON verbatim to the output path, then re-runs the script
  pipeline   profiles precommit and ci, report assembly
  github     `gh api` reads and writes (REST only; gh pr is used only for `gh pr merge --auto`)
  post       PR comment rendering (inline findings with markers, summary with collapsed nits)
  evals      reviewer eval: reviewer-change detection, case loading, scoring, summary block
The merge decision is never an LLM output: RV8 is a pure function of its inputs (spec.review_pipeline.principles).
"""
