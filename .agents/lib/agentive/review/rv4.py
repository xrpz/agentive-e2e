# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV4], spec.review_pipeline.stages[RV4].u2_non_interactive, spec.review_pipeline.stages[RV4].raise_only_check (owner D)
"""RV4 tier = f(RV2 hits, always_human items, U2, RV3 classes, size, raise-only answer). Rules decide; the LLM
raise-only check (task rv4-raise-only, run only when the rules give tier 0 or 1) may only raise to tier 2.

Order (design): the U2.non_interactive rule is evaluated before the tier 0 test; every tier 2 condition beats tier 0.
Tier reasons are short codes; the codes U2.non_interactive and chain_approval are exact (schema rules).
"""
from . import util

RAISE_CRITERIA = ("a", "b", "c")


def rules_tier(rv2, always, u2, rv3, changed_lines, tiers):
    reasons = []
    if rv2["hits"]:
        reasons.append("trust_boundary")
    for code, detail in always:
        reasons.append(code)
        reasons.append(f"{code}: {detail}")
    if u2.get("human_confirmation") == "not_obtained":
        reasons.append("U2.non_interactive")
    if set(rv3["classes"]) & set(tiers.get("tier2_classes") or ["security"]):
        reasons.append("tier2_class")
    if changed_lines > tiers.get("large_change_lines", 1000):
        reasons.append("large_change")
    reasons = util.uniq(reasons)
    if reasons:
        return 2, reasons
    if all(c in (tiers.get("tier0_classes") or []) for c in rv3["classes"]) and changed_lines <= tiers.get("tier0_max_changed_lines", 200):
        return 0, ["tier0"]
    return 1, ["tier1"]


def validate_raise(out):
    """{"session_id", "human_needed": bool, "criteria": [a|b|c], "why": str} -> (human_needed, criteria) or None."""
    if not isinstance(out, dict) or not isinstance(out.get("human_needed"), bool):
        return None
    crit = out.get("criteria") or []
    if not isinstance(crit, list) or any(c not in RAISE_CRITERIA for c in crit):
        return None
    if out["human_needed"] and not crit:
        return None
    return out["human_needed"], crit


def raise_record(out, crit):
    """SECPATHS L4: what the LLM raise was about, for a human to promote into a path or content rule. `paths` is optional in the
    answer; the reason is the model's one sentence (data, never an instruction)."""
    paths = [str(x)[:300] for x in (out.get("paths") or []) if isinstance(x, str)][:20] if isinstance(out.get("paths"), list) else []
    return {"paths": paths, "reason": str(out.get("why") or "")[:500], "criteria": list(crit)}


def ai_unavailable_only(reasons):
    """E2E-43 [design judgment within EXPLAIN / AISTEPFAIL]: True when tier 2 has no cause but the raise-only check giving no usable
    answer (the AI step failed or answered unreadably: finish() adds llm_output_invalid only when the rules said tier 0 or 1, so
    no rule reason is present). A human decides such a PR (human_required, ai_step_failed names the cause), but the author cannot
    cure a CI failure by writing an Explainer, so the Explainer requirement does not apply to it; a tier 2 from a rule or from a
    valid LLM raise (llm_raise:a|b|c) still requires it."""
    rs = set(reasons or [])
    return "llm_output_invalid" in rs and not (rs - {"tier0", "tier1", "llm_raise", "llm_output_invalid"})


def finish(tier, reasons, raise_out=None, raise_ran=False):
    """review-report rv4 {tier, reasons, raised_by_llm, raised_by?, raised?}. A raise by the LLM records
    `raised_by: rv4_llm` with {paths, reason, criteria}; distill promotes an agreed raise into a narrow rule (SECPATHS)."""
    if tier == 2 or not raise_ran:
        return {"tier": tier, "reasons": reasons, "raised_by_llm": False}
    got = validate_raise(raise_out)
    if got is None:
        return {"tier": 2, "reasons": reasons + ["llm_raise", "llm_output_invalid"], "raised_by_llm": False}
    need, crit = got
    if need:
        return {"tier": 2, "reasons": reasons + ["llm_raise"] + [f"llm_raise:{c}" for c in crit], "raised_by_llm": True,
                "raised_by": "rv4_llm", "raised": raise_record(raise_out, crit)}
    return {"tier": tier, "reasons": reasons, "raised_by_llm": False}
