# agentive-unit: lib.agentive
# design: spec.work_loop.ci_independence_check, spec.work_loop.independence, decisions.accepted[INDEPCI], decisions.accepted[BOTPR], decisions.accepted[WFCHANGE] (owner D)
"""CI job agentive-independence (deterministic). "Enforced in CI, attested locally."

Enforced here, from data the CI job itself holds:
  1 an agent PR (the body has an agentive-review block) needs >= 1 review that the ai-review job itself produced
    (reviews.json of its artifact: source ci) whose run.sha equals the PR head sha. A review entry in the PR body is
    written by the implementer's side and is never counted, whatever it says.
  2 an agent PR has the three all-tier headings (## Verification, ## Risk, ## How to revert) in this order
  3 the local review records in the PR body get a format check only (known values); executed_by, attestation and context
    are shown, never used for a merge decision
Exempt (result `exempt`, never auto-merged: tier 1+ and human_required with the code as reason, review/pipeline.py, rv8.py):
  - bot PR: author type Bot or a login ending [bot] (Dependabot, Renovate): the body is written by the tool -> bot_pr_exempt
  - human PR without an agentive-review block: not an agent PR -> human_pr_exempt [design judgment: agent-only heading check]
Workflow-validation skip (E2E-11, decisions.accepted[WFCHANGE]): when review/llmstate.workflow_skip finds that the Claude Code
action skipped this run because the run's .github/workflows/agentive-review.yml differs from the default branch's copy (the
first install PR, a workflow-change PR, a base that differs from the default branch, an old merge commit), rule 1 is not
required: the missing CI review becomes a note (`ci_review_skipped:<kind>`), every other rule still applies, and the gate
ends human_required for that reason (never auto-merged). Only then: a missing CI review for any other reason still fails.
The tier-2 ## Explainer is RV8's check, not this one.
"""
from . import attest
from . import pr_body
from . import util

BOT_PR_EXEMPT = "bot_pr_exempt"
HUMAN_PR_EXEMPT = "human_pr_exempt"


def is_bot(pr):
    login = str((pr or {}).get("author") or "")
    return str((pr or {}).get("author_type") or "") == "Bot" or login.endswith("[bot]")


def exemption(pr):
    """{code, detail} when the PR is exempt from the heading and CI-review rules, else None."""
    if not pr:
        return None
    if is_bot(pr):
        return {"code": BOT_PR_EXEMPT, "detail": f"{pr.get('author')} is a bot: its PR body is written by the tool; a human reviews and merges"}
    if not util.fenced_blocks(pr.get("body") or "", pr_body.BLOCK_INFO):
        return {"code": HUMAN_PR_EXEMPT, "detail": "no agentive-review block: not an agent PR; a human reviews and merges"}
    return None


def accepted_ci_reviews(ci_reviews, head_sha):
    """Reviews the CI job produced for this head: source ci, run.sha == head sha, a run id."""
    out = []
    for r in ci_reviews or []:
        run = r.get("run") if isinstance(r, dict) else None
        if isinstance(r, dict) and r.get("source") == "ci" and isinstance(run, dict) and run.get("run_id") \
                and head_sha and run.get("sha") == head_sha:
            out.append(r)
    return out


def format_problems(blk):
    """Format check of the local records in the PR body block (known values only)."""
    probs = []
    for i, r in enumerate(blk.get("reviews") or []):
        if not isinstance(r, dict):
            probs.append(f"reviews[{i}] is not a mapping")
            continue
        for key, allowed in (("context", attest.CONTEXT), ("executed_by", attest.EXECUTED_BY),
                             ("attestation", attest.ATTESTATION), ("tool", attest.TOOLS)):
            if r.get(key) is not None and r[key] not in allowed:
                probs.append(f"reviews[{i}].{key} must be one of {', '.join(allowed)}")
    return probs


def local_notes(blk):
    """One line per local review record: what it attests (shown, not enforced)."""
    out = []
    for r in (blk or {}).get("reviews") or []:
        if isinstance(r, dict) and r.get("source") != "ci":
            out.append(f"local review {r.get('reviewer_id') or r.get('role')}: executed_by={r.get('executed_by', 'unknown')} "
                       f"attestation={r.get('attestation', 'self_reported')} context={r.get('context', 'unverified')} (attested locally, not enforced)")
    return out


def evaluate(pr, ci_reviews=None, ci_skip=None):
    """{result: ok | fail | exempt, code?, detail?, problems, notes}. ci_skip: the llmstate.workflow_skip result or None."""
    ex = exemption(pr)
    if ex:
        return {"result": "exempt", "code": ex["code"], "detail": ex["detail"], "problems": [], "notes": []}
    body = pr.get("body") or ""
    blk, bprob = pr_body.block(body)
    probs = []
    if blk is None:
        probs.append(bprob or "agentive-review block is invalid")
    else:
        probs += format_problems(blk)
    probs += pr_body.all_tier_headings(body)
    notes = local_notes(blk)
    if not accepted_ci_reviews(ci_reviews, pr.get("head_sha")):
        if ci_skip and ci_skip.get("exempt"):
            notes.append(f"ci_review_skipped:{ci_skip['kind']}: no CI review for the head {str(pr.get('head_sha'))[:12]}: "
                         f"{ci_skip['detail']} (not required for this PR; the gate sends it to a human)")
        else:
            probs.append(f"no review produced by the CI job for the PR head {str(pr.get('head_sha'))[:12]} "
                         f"(source ci, run.sha equal to the head sha); a review in the PR body is not counted")
    return {"result": "fail" if probs else "ok", "problems": probs, "notes": notes}
