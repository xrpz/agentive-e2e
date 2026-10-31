# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV8], spec.review_pipeline.auto_merge_policy, spec.review_pipeline.convergence.cap, spec.review_pipeline.severity_blocking.reply_line.disputed, spec.work_loop.pr_explainer.enforcement (owner D)
"""RV8 policy gate: a pure function of its inputs -> review-report rv8 (block | human_required | pass + auto_merge).

Evaluation order (design rules, top first):
  1 block: a failing required check (incl. agentive-independence), a confirmed important finding without a fix or a
    reasoned reply (a 'fixed' reply on the reviewed head is a claim and still blocks)
  2 block: tier 2 without a valid ## Explainer (explainer_missing), chain order problems (chain_order); both before 3
  3 human_required: tier 2, an RV2 hit, an inconclusive important finding, finding_disputed, review_rounds_exceeded,
    pending verbs among the required checks, an invalid LLM output of RV5 or RV6 [design judgment: fail-safe like RV3/RV4],
    a PR exempt from the independence check (bot_pr_exempt, human_pr_exempt: never auto-merged, V2-08)
  4 gate-only conditions block auto-merge without raising the tier: public API breaking change, test-only deletion,
    new-code coverage below the threshold (coverage_unavailable is recorded, never blocks)
  5 review.auto_merge (off: every PR to a human; dry_run: nothing merges, a PR that would have merged gets the label
    agentive:shadow-auto-merge; low_risk (the default, U1=C): tier 0 merges, a qualifying tier 1 PR gets the label; low_and_medium_risk: tier 0
    and tier 1 merge, tier 1 only with an RV7b score at or below the threshold), then the kill switch (demoted scope ->
    human_required). The setting is a framework default overridden in .agents/framework.yaml (DEFAULTS, V1-18)
review_rounds_exceeded [design judgment]: from round 3 on, an open blocking finding turns the unanswered-finding block into
human_required (the author rethinks the approach, a human decides); failing checks still block.
"""
from .. import common
from . import util
from .config import AUTO_MERGE_DEFAULT, AUTO_MERGE_LABEL

ROUND_CAP = 3


def pending_text(verbs):
    """The pending_verbs detail (V2-23, PENDINGNOTICE): what blocks auto-merge and the two ways out."""
    verbs = list(verbs)
    which = "check" if len(verbs) == 1 else "checks"
    return (f"human review needed: required {which} {common.no_command(verbs)} (pending), so nothing verifies "
            f"{'it' if len(verbs) == 1 else 'them'}. "
            f"Give the command (run /agentive:update after telling your agent the command) or, if this project has no such check, "
            f"remove it from review.pipeline.stages.RV1.require_checks in .agents/framework.yaml.")


def decide(inp):
    """inp keys: tier, rv4_reasons, rv2, u2, checks, required_checks, findings, rv7b, auto_merge (off | dry_run | low_risk |
    low_and_medium_risk), config_valid,
    explainer (present, problems) or None, chain, review_round, pending_verbs, signals, llm_problems, head_sha,
    trigger, kill_switch (callable(tier) -> {count, demoted, ...}) or None, source, exempt ({code, detail} or None)."""
    reasons, codes = [], []

    def add(code, detail=None):
        # reasons hold the bare code (schema rules match it exactly) and, when there is one, a detail line
        codes.append(code)
        reasons.append(code)
        if detail:
            reasons.append(f"{code}: {detail}")

    tier = inp["tier"]
    findings = inp.get("findings") or []
    setting = inp.get("auto_merge", AUTO_MERGE_DEFAULT)
    out = {"auto_merge_setting": setting, "head_sha": inp["head_sha"], "auto_merge": False}
    # ---- 1 block
    block = False
    failing = [c["name"] for c in inp.get("checks") or [] if c["status"] == "fail"]
    if failing:
        block = True
        add("failing_required_check", ", ".join(failing))
    open_blocking = [f for f in findings if f.get("blocking") and not _answered(f)]
    round_no = (inp.get("review_round") or {}).get("round", 1)
    if open_blocking:
        if round_no >= ROUND_CAP:
            add("review_rounds_exceeded", f"round {round_no} with {len(open_blocking)} open blocking finding(s): rethink the approach; a human decides")
        else:
            block = True
            add("unanswered_finding", ", ".join(f["id"] for f in open_blocking) + " (fix it, or reply with an agentive-reply line)")
    # ---- 2 explainer / chain
    if tier == 2:
        present, problems = inp.get("explainer") or ({}, ["no_section"])
        if problems:
            block = True
            add("explainer_missing", ", ".join(problems))
            out["explainer"] = {"required": True, "result": "explainer_missing", "problems": problems, "present": _present(present),
                                "next_action": "run /pr --explain (it rewrites only ## Explainer), or edit the PR body; a body edit re-runs the gate"}
        else:
            out["explainer"] = {"required": True, "result": "ok", "problems": [], "present": _present(present)}
    else:
        out["explainer"] = {"required": False, "result": "not_required"}
    if inp.get("trigger"):
        out["explainer"]["trigger"] = inp["trigger"]
    chain = inp.get("chain") or {}
    if chain.get("result") == "chain_order":
        block = True
        add("chain_order", chain.get("next_action"))
    # ---- 3 human_required
    human = False
    exempt = inp.get("exempt")
    if exempt:
        human = True
        add(exempt["code"], exempt.get("detail"))
    if tier == 2:
        human = True
        add("tier2")
        for r in inp.get("rv4_reasons") or []:
            if r in ("U2.non_interactive", "chain_approval", "always_human", "llm_raise", "critical_cve_unchecked", "trust_boundary"):
                add(r)
    if (inp.get("rv2") or {}).get("hits"):
        human = True
        add("trust_boundary")
    if (inp.get("u2") or {}).get("action") == "pr_only_tier2":
        human = True
        add("U2.non_interactive")
    for f in findings:
        v = (f.get("verification") or {}).get("verdict")
        if f["severity"] == "important" and not f.get("shadow") and v == "inconclusive" and (f.get("response") or {}).get("source") != "next_round":
            human = True
            add("inconclusive_finding", f["id"])
        if f["severity"] == "important" and v == "confirmed" and (f.get("response") or {}).get("reason") in ("wrong", "intentional", "not_important"):
            human = True
            add("finding_disputed", f"{f['id']} answered {f['response']['reason']}")
    if open_blocking and round_no >= ROUND_CAP:
        human = True
    pend = [v for v in inp.get("pending_verbs") or [] if v in (inp.get("required_checks") or [])]
    if pend:
        human = True
        add("pending_verbs", pending_text(pend))
    for p in inp.get("llm_problems") or []:
        human = True
        add("llm_raise", p)
    # ---- 4 gate-only
    sig = inp.get("signals") or {}
    gate_only = []
    if sig.get("public_api_change") == "breaking":
        gate_only.append("public_api_breaking")
    if sig.get("test_only_deletion"):
        gate_only.append("test_only_deletion")
    cov = sig.get("coverage") or {}
    if cov.get("status") == "below_threshold":
        gate_only.append("coverage_below_threshold")
    elif cov.get("status") == "coverage_unavailable":
        add("coverage_unavailable", "new-code coverage skipped (no coverage report)")
    if gate_only:
        human = True
        add("gate_only", ", ".join(gate_only))
    out["gate_only"] = gate_only
    # ---- 5 review.auto_merge and kill switch
    rv7b = inp.get("rv7b") or {}
    merge_now = False
    if not block and not human:
        eligible = False
        if setting == "off":
            add("auto_merge_off", "review.auto_merge is off: every PR goes to a human")
        elif tier == 0:
            eligible = True
        elif tier == 1:
            if rv7b.get("reason") == "config_invalid":
                add("rubric_invalid", "the rubric override does not validate: tier 1 goes to a human")
            elif not inp.get("config_valid", True) or rv7b.get("reason") == "tier1_off":
                add("tier1_off", "the review config does not validate: tier 1 goes to a human")
            elif rv7b.get("score") is None:
                add("rubric_invalid", f"no score ({rv7b.get('reason')})")
            elif rv7b["score"] > rv7b["human_if_score_gt"]:
                add("score_above_threshold", f"score {rv7b['score']} > {rv7b['human_if_score_gt']}")
            else:
                eligible = True
        if eligible:
            merges = {"low_risk": tier == 0, "low_and_medium_risk": tier in (0, 1)}.get(setting, False)
            if _not_demoted(inp, tier, out):
                if merges:
                    merge_now = True
                else:
                    out["would_auto_merge"] = True
                    add("would_auto_merge", f"review.auto_merge is {setting}: this tier {tier} PR would have been merged; "
                                            f"label {AUTO_MERGE_LABEL}, a human merges")
            else:
                add("demoted", f"auto-merge is stopped for tier {tier} and source {inp.get('source')} (open issue labeled agentive:auto-merge-demoted)")
    if merge_now:
        out["auto_merge"] = True
    if "demoted" not in out:
        out["demoted"] = False
    if block:
        out["decision"], out["check_conclusion"], out["auto_merge"] = "block", "failure", False
    elif out["auto_merge"]:
        out["decision"], out["check_conclusion"] = "pass", "success"
    else:
        out["decision"], out["check_conclusion"] = "human_required", "success"
    out["reasons"] = util.uniq(reasons)
    out["reason_codes"] = util.uniq(codes)
    return out


def _answered(f):
    r = f.get("response") or {}
    return r.get("source") == "next_round" or r.get("reason") in ("wrong", "intentional", "not_important")


def _present(p):
    keys = ("section", "first", "before", "after", "before_mermaid", "after_mermaid")
    return {k: bool((p or {}).get(k)) for k in keys}


def _not_demoted(inp, tier, out):
    ks = inp.get("kill_switch")
    if ks is None:
        out["kill_switch"] = {"tier": tier, "source": inp.get("source") or "unknown", "count": 0}
        return True
    r = ks(tier)
    out["kill_switch"] = {"tier": tier, "source": inp.get("source") or "unknown", "count": r["count"]}
    out["demoted"] = bool(r["demoted"])
    out["_kill_switch_result"] = r
    return not r["demoted"]
