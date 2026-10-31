# agentive-unit: lib.agentive
# design: spec.review_pipeline.severity_blocking (nit, answer, reply_line.finding_marker), spec.review_pipeline.convergence (owner D)
"""PR comment text. Important findings are posted inline (they carry file:line) with the finding marker; nits and
pre_existing findings go only to review-report.json and a collapsed section of the summary comment (never inline,
never sent to the author agent as fix requests, F1e). Refuted findings are recorded, not posted.
"""
import json

from . import config as cfgmod
from . import findings as fmod

SUMMARY_MARKER = "<!-- agentive-review-summary -->"


def finding_comment(f, round_no, head_sha=None):
    v = f.get("verification") or {}
    lines = [f"**[{f['severity']}] {f['summary']}**" + (f" ({f['bug_class']}" + (f": {f['pattern']}" if f.get("pattern") else "") + ")" if f.get("bug_class") else ""), "",
             f"- Trigger: {f['trigger']}"]
    if f.get("evidence"):
        lines.append(f"- Evidence: {f['evidence']}")
    if f.get("instances"):
        lines.append("- Same bug class at: " + ", ".join(f"`{i['file']}:{i['line']}`" for i in f["instances"]))
    lines.append(f"- Reviewers: {', '.join(f['reviewers'])}; verifier: {v.get('verdict', 'not run')}"
                 + (f" ({v['method']})" if v.get("method") else ""))
    if v.get("verdict") == "inconclusive":
        lines.append("- Inconclusive: not blocking by itself; a human decides (RV8 human_required).")
    lines += ["", "Answer in this thread: push a fix, or reply with one line "
              f"`agentive-reply: {f['id']} <fixed|wrong|intentional|not_important> <one sentence>` "
              "(a sentence is required for wrong, intentional and not_important).",
              "", f"<!-- agentive-finding: {f['id']} -->",
              f"<!-- agentive-finding-data: {json.dumps(fmod.finding_data(f, round_no, head_sha), ensure_ascii=False, sort_keys=True)} -->"]
    return "\n".join(lines)


def auto_merge_line(rv8):
    """One line for the gate output and the summary comment: the effective review.auto_merge value, what it does for this
    PR and how to change it (DEFAULTS). No policy ids: the user never needs to know U1."""
    rv8 = rv8 or {}
    setting = rv8.get("auto_merge_setting") or cfgmod.AUTO_MERGE_DEFAULT
    default = " (framework default)" if setting == cfgmod.AUTO_MERGE_DEFAULT else ""
    this = ""
    if rv8.get("auto_merge"):
        this = " This PR is set to merge automatically once GitHub's merge requirements are met."
    elif rv8.get("would_auto_merge"):
        this = f" This PR would have been merged automatically (label {cfgmod.AUTO_MERGE_LABEL})."
    return (f"- auto merge: {setting}{default}: {cfgmod.AUTO_MERGE_MEANING.get(setting, 'unknown value')}.{this} "
            f"To change it, tell your agent or set review.auto_merge in .agents/framework.yaml "
            f"(off | dry_run | low_risk | low_and_medium_risk); a merge also needs the GitHub repository setting 'Allow auto-merge'.")


def summary_comment(report, posted_now, extras):
    rv3, rv4, rv7b = report.get("rv3") or {}, report.get("rv4") or {}, report.get("rv7b") or {}
    rnd = report.get("review_round") or {}
    fnd = report.get("findings") or []
    imp = [f for f in fnd if f["severity"] == "important" and (f.get("verification") or {}).get("verdict") != "refuted"]
    minor = [f for f in fnd if f["severity"] in ("nit", "pre_existing") and (f.get("verification") or {}).get("verdict") != "refuted"]
    refuted = [f for f in fnd if (f.get("verification") or {}).get("verdict") == "refuted"]
    out = [SUMMARY_MARKER, f"### agentive review (round {rnd.get('round', 1)}, {rnd.get('mode', 'full')})", "",
           f"- head: `{report['head_sha'][:12]}` · classes: {', '.join(rv3.get('classes') or [])} ({rv3.get('method', '-')})"
           f" · tier: {rv4.get('tier', '-')} ({', '.join(rv4.get('reasons') or [])})",
           f"- risk score: {rv7b.get('score') if rv7b.get('score') is not None else '-'} ({rv7b.get('reason', '-')})",
           f"- important findings: {len(imp)} (blocking {sum(1 for f in imp if f['blocking'])}, posted inline this round "
           f"{posted_now}); refuted by the verifier: {len(refuted)}",
           "- the merge decision is the required check `agentive-review-gate`"]
    if report.get("rv8"):
        out.append(auto_merge_line(report["rv8"]))
        pv = [r for r in report["rv8"].get("reasons") or [] if r.startswith("pending_verbs: ")]
        if pv:
            out.append("- " + pv[0][len("pending_verbs: "):])
    if imp:
        out += ["", "| id | file:line | verdict | answer |", "|---|---|---|---|"]
        for f in imp:
            r = f.get("response") or {}
            out.append(f"| {f['id']} | `{f['file']}:{f['line']}` | {(f.get('verification') or {}).get('verdict', '-')} | "
                       f"{r.get('reason', 'open')}{' (' + r['source'] + ')' if r.get('source') else ''} |")
    if minor:
        out += ["", f"<details><summary>{len(minor)} nit / pre-existing note(s) (not blocking, no action needed)</summary>", ""]
        for f in minor:
            out.append(f"- [{f['severity']}] `{f['file']}:{f['line']}` {f['summary']}")
        out += ["", "</details>"]
    if (extras or {}).get("ai_step_failed"):
        out.append(f"- {extras['ai_step_failed']}")      # E2E-10b: code, fixed sentence and run URL only
    for p in (extras or {}).get("llm_problems") or []:
        out.append(f"- note: {p}")
    return "\n".join(out) + "\n"
