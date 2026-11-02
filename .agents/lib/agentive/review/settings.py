# agentive-unit: lib.agentive
# design: decisions.accepted[DEFAULTS], decisions.accepted[PENDINGNOTICE], decisions.accepted[EFFECTIVECHECKS], spec.review_pipeline.config, contracts[project-config] (owner D)
"""`agentive-lint settings --effective`: the settings that are in force, where each value comes from and, for auto-merge,
why it is not available. Defaults live in framework code; .agents/framework.yaml holds the user's overrides only (DEFAULTS).
Pure text from a policy tree: source(path) -> text | None (the working tree for the command, the base commit in CI)."""
from .. import ayaml
from . import config as cfgmod


def _doc(source):
    text = source(".agents/framework.yaml")
    if text is None:
        return {}
    try:
        d = ayaml.loads(text) or {}
    except ayaml.YAMLError:
        return {}
    return d if isinstance(d, dict) else {}


def effective(source):
    """-> list of output lines."""
    am = cfgmod.load_auto_merge(source)
    conf_l = cfgmod.load_project_config(source)
    conf = conf_l.value
    manifest = cfgmod.load_manifest(source).value
    doc = _doc(source)
    review = doc.get("review") if isinstance(doc.get("review"), dict) else {}
    pipe = review.get("pipeline") if isinstance(review.get("pipeline"), dict) else {}
    out = ["AGENTIVE_SETTINGS effective settings: agentive defaults, overridden by what .agents/framework.yaml sets"]
    out.append(f"review.auto_merge: {am.value}   [{am.source}]")
    out.append(f"  {cfgmod.AUTO_MERGE_MEANING[am.value]}")
    out.append(f"  {cfgmod.AUTO_MERGE_RULES}")
    out.append("  values: " + " | ".join(cfgmod.AUTO_MERGE_VALUES) + f" (default {cfgmod.AUTO_MERGE_DEFAULT}); change it by telling your agent "
               "or editing .agents/framework.yaml (a review config change: a human confirms the commit)")
    for p in am.problems:
        out.append(f"  problem: {p}")
    rs = cfgmod.required_state_of(manifest, conf_l)
    pend = rs["pending_required"]
    if pend and am.value != "off":
        out.append(f"  unavailable: {cfgmod.unavailable_text(pend)}")
        out.append(f"  or: {cfgmod.REQUIRE_CHECKS_EXIT}")
    elif rs["floor"] and am.value != "off":
        out.append(f"  unavailable: {cfgmod.floor_text(rs['required'])}")
    out.append("  GitHub: the repository setting 'Allow auto-merge' (Settings > General > Pull Requests) is not readable here; "
               "without it nothing is merged even when this value allows it")
    out.append("review.pipeline.stages.RV1.require_checks: " + ", ".join(((conf.get("stages") or {}).get("RV1") or {}).get("require_checks") or [])
               + f"   [{rs['source']}]")
    # E2E-19: the effective list is computed (require_checks minus the pending verbs of the default list); nothing is written
    out.append("  in force: " + (", ".join(rs["required"]) or "none") + "; " + cfgmod.not_required_text(rs["not_required"])
               if rs["not_required"] else "  in force: " + (", ".join(rs["required"]) or "none"))
    tool = cfgmod.ci_tool(pipe, manifest)
    via = "review.pipeline.ci_tool in .agents/framework.yaml" if pipe.get("ci_tool") in cfgmod.CI_TOOLS else "the tool that ran init (agentive manifest framework.init_tool)"
    out.append(f"review.pipeline.ci_tool: {tool}   [{via}]")
    if tool == "claude":
        auth = cfgmod.ci_auth(pipe, tool)
        names = " and ".join(c["secret"] for _, c in cfgmod.ci_credentials(tool, auth))
        out.append(f"review.pipeline.ci_auth: {auth}   [{'.agents/framework.yaml' if pipe.get('ci_auth') in cfgmod.CI_AUTH else 'framework default'}]")
        out.append(f"  the ai-review and reviewer-eval workflows pass the repository secret(s) {names}"
                   + ("; OAuth token first: ANTHROPIC_API_KEY is passed only when CLAUDE_CODE_OAUTH_TOKEN is empty" if auth == "both" else ""))
    mc = review.get("managed_code_review_copy")
    out.append(f"review.managed_code_review_copy: {'true' if mc is True else 'false'}   [{'.agents/framework.yaml' if 'managed_code_review_copy' in review else 'framework default'}]")
    if source(".agents/decisions.yaml") is not None:
        out.append("note: .agents/decisions.yaml is no longer read (DEFAULTS); run /agentive:update to move its values into .agents/framework.yaml")
    if isinstance(pipe.get("auto_merge"), dict) and "tier1" in pipe["auto_merge"]:
        out.append("note: review.pipeline.auto_merge.tier1 is ignored (replaced by review.auto_merge); delete the key")
    for p in conf_l.problems:
        out.append(f"problem: {p}")
    return out
