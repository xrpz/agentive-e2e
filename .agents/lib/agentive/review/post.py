# agentive-unit: lib.agentive
# design: spec.review_pipeline.severity_blocking (nit, answer, reply_line.finding_marker), spec.review_pipeline.convergence, decisions.accepted[EFFECTIVECHECKS], decisions.accepted[WFCHANGE], decisions.accepted[EVALOVERRIDE], decisions.accepted[LOCALTIER], decisions.accepted[COMMENTUX] (owner D)
"""PR comment text. Important findings are posted inline (they carry file:line) with the finding marker; nits and
pre_existing findings go only to review-report.json and a collapsed section of the summary comment (never inline,
never sent to the author agent as fix requests, F1e). Refuted findings are recorded, not posted.
"""
import json
import re

from . import config as cfgmod
from . import findings as fmod
from . import llmstate
from . import util

SUMMARY_MARKER = "<!-- agentive-review-summary -->"


def loc(f):
    """file:line of a finding; a file-level finding (E2E-39) shows the file alone."""
    return f["file"] if f.get("file_level") else f"{f['file']}:{f['line']}"


def finding_comment(f, round_no, head_sha=None):
    v = f.get("verification") or {}
    lines = [f"**[{f['severity']}] {f['summary']}**" + (f" ({f['bug_class']}" + (f": {f['pattern']}" if f.get("pattern") else "") + ")" if f.get("bug_class") else ""), "",
             f"- Trigger: {f['trigger']}"]
    if f.get("evidence"):
        lines.append(f"- Evidence: {f['evidence']}")
    if f.get("instances"):
        lines.append("- Same bug class at: " + ", ".join(f"`{i['file']}:{i['line']}`" for i in f["instances"]))
    if f.get("line_note"):                 # E2E-39: the reviewer's line was outside the file
        lines.append(f"- Location: {f['line_note']}")
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


CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")


def reason_lines(reasons):
    """reasons hold a bare code and, when there is one, 'code: detail' (rv8.decide, RV4). -> one line per reason, each code
    once: the detail sentence when it is one ('human review needed: ...'), else 'code: detail', else the code (E2E-19,
    E2E-24: every reason is shown, none repeated) [design judgment: the wording]."""
    return [text for _, text in reason_items(reasons)]


def tier_reasons(rv4_reasons, listed):
    """E2E-31: the tier line's reasons. A reason whose line is already in the `not merged automatically` list (`listed`) is
    shown there only and stands here as its code, so a long sentence such as the dependency_change cause appears once."""
    shown = set(listed or [])
    return util.uniq([code if text in shown else text for code, text in reason_items(rv4_reasons)])


def reason_items(reasons):
    """[(code, line)] in the order of reason_lines."""
    order, details = [], {}
    for r in reasons or []:
        code, sep, det = str(r).partition(": ")
        if not (sep and CODE.match(code)):
            code, det = str(r), ""
        if code not in details:
            order.append(code)
            details[code] = []
        if det and det not in details[code]:
            details[code].append(det)
    out = []
    for code in order:
        if not details[code]:
            out.append((code, code))
        for det in details[code]:
            out.append((code, det if det.startswith("human review needed") else f"{code}: {det}"))
    return out


NOT_RUN = re.compile(r"^[a-z0-9][a-z0-9-]*: (?:not run|no output)$")     # pipeline.py: an LLM task with no output


def skipped_view(reasons, problems, skip, rv4_reasons=()):
    """E2E-26: (reasons, notes) to show when the action skipped the AI review (llmstate.workflow_skip: skip = {kind, detail}).
    The missing task outputs (`<task>: not run | no output`) are a consequence of the skip, so they leave the llm_raise reasons
    and the notes, and the skip is shown once as `ai_review_skipped: <kind>: <detail>`, the gate's reason (E2E-11) [design
    judgment: wording]. A bare llm_raise stays when RV4 raised it. Display only: the recorded rv8 reasons are unchanged."""
    reasons, problems = list(reasons or []), list(problems or [])
    if not skip:
        return reasons, problems
    gone = {p for p in problems if NOT_RUN.match(p)}
    out = [r for r in reasons if not (r.startswith("llm_raise: ") and r[len("llm_raise: "):] in gone)]
    if "llm_raise" not in (rv4_reasons or []) and not any(r.startswith("llm_raise: ") for r in out):
        out = [r for r in out if r != "llm_raise"]
    if "ai_review_skipped" not in out:
        out += ["ai_review_skipped", f"ai_review_skipped: {skip['kind']}: {skip['detail']}"]
    return out, [p for p in problems if p not in gone]


def _required_state(report):
    """rv8.required_checks for display: the first-install display copy (E2E-34) when there is one, else the gate's own."""
    rc = ((report or {}).get("rv8") or {}).get("required_checks")
    if not rc:
        return None
    return rc.get("display") or rc


def required_lines(report):
    """The required checks in force and, as an info line, the pending verbs of the default list (E2E-19). Old reports
    without rv8.required_checks give no line. E2E-34: on the first install the lines come from the PR's own manifest (display
    only; the gate decided from the base)."""
    rc = _required_state(report)
    if not rc:
        return []
    st = {c.get("name"): c.get("status") for c in ((report.get("rv1") or {}).get("checks") or []) if isinstance(c, dict)}
    out = ["- required checks: " + (", ".join(f"{v} ({st.get(v, 'not reported')})" for v in rc.get("required") or []) or "none")
           + f" [{rc.get('source', 'framework default')}]"]
    if rc.get("not_required"):
        out.append("- " + cfgmod.not_required_text(rc["not_required"]))
    return out


# ================================================================ COMMENTUX (decisions.accepted[COMMENTUX], 2026-11-01)
# A human reads the summary comment, the reviewer-eval comment and the gate step summary in this order: verdict -> what to
# do -> why (plain sentences, grouped) -> checks and findings (one line each); codes, lists and the auto-merge setting are
# folded in <details>. The words below are [design judgment: wording]; every visible sentence names no internal code.
ICON = {"ok": "✅", "warn": "⚠️", "block": "⛔", "info": "ℹ️"}
WHY_MAX = 5
STATUS_WORD = {"pass": "passed", "fail": "failed", "skipped": "not run", "skip": "not run", "cancelled": "not run"}
HIT_NAMES = {"auth": "auth", "security": "security", "secrets": "secrets", "permissions": "permissions",
             "db_migration": "database migrations", "ci_config": "CI workflows", "agent_hooks_settings": "agent hooks and settings",
             "billing": "billing", "check_config": "review/gate config"}
SCOPE_NAMES = {"hooks": "hooks", "permissions": "permissions", "ci": "CI workflows", "claude_settings": "Claude Code settings",
               "codex_dir": "Codex settings", "review_config": "review config"}
HIDDEN = {"tier0", "tier1", "tier2", "always_human", "llm_raise", "dependency_change", "ai_review_skipped", "ai_step_failed",
          "coverage_unavailable", "would_auto_merge"}
AI_STEP_WHY = {
    "insufficient_credits": "The AI review step failed: the CI credential's Claude API account has too little credit.",
    "auth": "The AI review step failed: the CI credential was rejected.",
    "rate_limit": "The AI review step failed: the model API was rate-limited or overloaded.",
    "timeout": "The AI review step timed out.",
    "context_too_long": "The AI review step failed: the change is too large for the model context.",
    "max_turns": "The AI review step hit its turn limit before finishing.",
    "no_result": "The AI review step ended without a result.",
    "unclassified": "The AI review step failed for a reason agentive does not classify.",
    "codex_exit": "The AI review step (codex exec) exited with an error.",
    "no_secret": "The AI review could not run: this repository has no AI review credential secret.",
    "fork_pr": "The AI review could not run: this PR comes from a fork, and GitHub gives fork runs no repository secrets.",
}
RAISE_WHY = {
    "a": "The AI check asked for a human: the PR decides whether or how far to build a capability.",
    "b": "The AI check asked for a human: the PR touches high-damage code (billing, deploy, installer, release, data deletion, "
         "auth or secrets).",
    "c": "The AI check asked for a human: the PR sets structure others will copy (a new module or shared abstraction).",
}
EXPLAINER_PROBLEMS = {"no_section": "missing", "not_first": "it must come first", "no_before": "no Before part",
                      "no_after": "no After part", "before_order": "Before must come before After"}
GATE_ONLY = {"public_api_breaking": "It changes a public API in a breaking way.",
             "test_only_deletion": "It deletes tests without changing the code they test.",
             "coverage_below_threshold": "New-code test coverage is below the limit."}
NO_RAW_HINT = llmstate.NO_RAW_HINT      # the cause is known without the log (codex_exit, no_secret, fork_pr)
AI_STEP_CODE = re.compile(r"ai_step_failed:([a-z_]+)")
RUN_URL = re.compile(r"\(run (\S+)\)\s*$")
DEP_RE = re.compile(r"dependency change \((?P<what>.*)\), never auto-merged(?:; (?P<review>dependency review .*?)(?: \(information only\))?)?\.?$")
ENV_RE = re.compile(r"^env vars? added or removed(?: \((\d+)\))?")


def _details(reasons, code):
    return [d for c, d in _split(reasons) if c == code and d]


def _split(reasons):
    out = []
    for r in reasons or []:
        code, sep, det = str(r).partition(": ")
        if not (sep and CODE.match(code)):
            code, det = str(r), ""
        out.append((code, det))
    return out


def _few(names, n=3):
    names = list(names)
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def _plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _counts(items, key, names):
    c = {}
    for it in items:
        c[it[key]] = c.get(it[key], 0) + 1
    order = sorted(c, key=lambda k: (-c[k], k))
    return [(names.get(k, k), c[k]) for k in order]


def _hit_paths(rv2):
    """{reason: [paths]} of the RV2 hits, each path once per reason."""
    out = {}
    for h in (rv2 or {}).get("hits") or []:
        lst = out.setdefault(h.get("reason") or "other", [])
        if h.get("path") not in lst:
            lst.append(h.get("path"))
    return out


def _skip(extras):
    s = (extras or {}).get("ai_review_skipped")
    return s if isinstance(s, dict) and s.get("kind") else None


def _first_install(skip):
    return bool(skip and skip.get("kind") == "workflow_change" and "first install" in (skip.get("detail") or ""))


def _ai_failure(extras):
    """(code, run url, fixed sentence) of the E2E-10b failure text in extras, or None."""
    text = (extras or {}).get("ai_step_failed") or ""
    m = AI_STEP_CODE.search(text)
    if not m:
        return None
    u = RUN_URL.search(text)
    s = re.search(r"ai_step_failed:[a-z_]+\): (.*?)(?:; no error text is copied here.*?)?(?: \(run \S+\))?$", text)
    return m.group(1), (u.group(1) if u else None), (s.group(1) if s else None)


def why_items(report, extras=None, reasons=None):
    """COMMENTUX groups G1-G12 -> [{group, text, codes, block}] in reading order (block reasons, then the human reasons, then
    information). reasons: the rv8 reasons as shown (skipped_view applied); the always_human details come from rv4."""
    report, extras = report or {}, extras or {}
    rv8, rv4, rv2 = report.get("rv8") or {}, report.get("rv4") or {}, report.get("rv2") or {}
    rv1 = report.get("rv1") or {}
    reasons = list(reasons if reasons is not None else rv8.get("reasons") or [])
    allr = reasons + [r for r in rv4.get("reasons") or [] if r not in reasons]
    codes = util.uniq(c for c, _ in _split(allr))
    items, used = [], set()

    def add(group, text, cs, block=False):
        items.append({"group": group, "text": text, "codes": list(cs), "block": block})
        used.update(cs)

    # ---- block reasons (the gate fails)
    for d in _details(reasons, "failing_required_check"):
        add("G7", f"Required check failed: {d}.", ["failing_required_check"], True)
    for d in _details(reasons, "unanswered_finding"):
        ids = d.split(" (")[0]
        add("G8", f"{_plural(len(ids.split(', ')), 'blocking AI finding')} {'has' if len(ids.split(', ')) == 1 else 'have'} "
                  f"no fix or answer yet: {ids}.", ["unanswered_finding"], True)
    for d in _details(reasons, "explainer_missing"):
        probs = [EXPLAINER_PROBLEMS.get(p.strip(), p.strip()) for p in d.split(",")]
        add("G9", f"The PR body needs a ## Explainer section ({'; '.join(probs)}).", ["explainer_missing"], True)
    for d in _details(reasons, "chain_order"):
        add("G9", f"The plan steps are out of order: {d}", ["chain_order"], True)
    # ---- G5 the AI review did not run or failed
    skip = _skip(extras)
    if skip:
        k = skip["kind"]
        text = {"fork": "This PR comes from a fork, and GitHub gives fork runs no secrets, so the AI review cannot run.",
                "workflow_change": ("It adds the CI workflow `.github/workflows/agentive-review.yml`. The Claude Code action runs only with "
                                    "the default branch's copy of that file, so the AI review was skipped." if _first_install(skip) else
                                    "It changes `.github/workflows/agentive-review.yml`. The Claude Code action runs only with the default "
                                    "branch's copy, so the AI review was skipped."),
                "base_differs": "The base branch has a different review workflow from the default branch, so the AI review was skipped.",
                "stale_merge": "This run used an older review workflow than the default branch has now, so the AI review was skipped.",
                }.get(k, "The AI review was skipped.")
        add("G5", text, ["ai_review_skipped"])
    fail = _ai_failure(extras)
    if fail:
        add("G5", AI_STEP_WHY.get(fail[0], AI_STEP_WHY["unclassified"]), ["ai_step_failed"])
    if "ai_review_missing" in codes:
        add("G5", "The AI review job left no report for this commit (it failed or did not run).", ["ai_review_missing"])
    for d in _details(reasons, "llm_raise"):
        add("G5", f"An AI reviewer step gave no usable output ({d}); a human reviews instead.", ["llm_raise"])
    if "llm_output_invalid" in codes:
        add("G5", "The AI risk check returned an unreadable answer, so a human decides.", ["llm_output_invalid"])
    for c in ("a", "b", "c"):
        if f"llm_raise:{c}" in codes:
            add("G5", RAISE_WHY[c], [f"llm_raise:{c}"])
    # ---- G2 security-sensitive paths, G3 confirmation
    hp = _hit_paths(rv2)
    if hp or "trust_boundary" in codes or "tier2_class" in codes:
        n_files = (rv1.get("diff_stats") or {}).get("files")
        paths = {p for ps in hp.values() for p in ps}
        ranked = sorted(hp.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:3]
        # E2E-38: one kind alone is named without its count (the count is the file count); several kinds keep `name count`
        top = HIT_NAMES.get(ranked[0][0], ranked[0][0]) if len(hp) == 1 else \
            ", ".join(f"{HIT_NAMES.get(r, r)} {len(ps)}" for r, ps in ranked)
        if paths and n_files and len(paths) == n_files:
            text = f"It changes security-sensitive files: all {n_files} {'file is' if n_files == 1 else 'files are'} {top}" \
                if len(hp) == 1 else f"It changes security-sensitive files (all {n_files} files: {top})"
        elif paths:
            text = f"It changes security-sensitive files ({len(paths)}{f' of {n_files}' if n_files else ''} files: {top})"
        if paths:
            text += "; a file is classified as a security change." if "tier2_class" in codes else "."
        else:
            text = "A changed file is classified as a security change."
        add("G2", text, ["trust_boundary", "tier2_class"])
    prot = [d for d in _details(allr, "always_human") if d.startswith("gate or agent config change")]
    if prot:
        used.add("always_human")
    if "U2.non_interactive" in codes:
        add("G3", "It changes hooks, permissions, CI or agent settings, and no human confirmed that during the agent session.",
            ["U2.non_interactive"])
    # ---- G6 dependencies
    for d in _details(allr, "dependency_change"):
        m = DEP_RE.search(d)
        if m:
            add("G6", f"Dependencies changed: {m.group('what')}.", ["dependency_change"])
            if m.group("review"):         # E2E-31: the state in plain words, as the RV2 detail states it (one sentence)
                rv = m.group("review")
                add("G6", rv[0].upper() + rv[1:] + (" (information only)" if d.rstrip(".").endswith("(information only)") else "") + ".",
                    ["dependency_change"])
        else:
            add("G6", d, ["dependency_change"])
        break
    # ---- always_human details (G1 env vars, G4 size, G11, G12 and the rest)
    env = (rv1.get("signals") or {}).get("env_vars_changed") or []
    env_done, later = False, []
    for d in _details(allr, "always_human"):
        if d.startswith("gate or agent config change"):
            continue
        m = ENV_RE.match(d)
        if m:
            if not env_done:
                n = int(m.group(1)) if m.group(1) else (len(env) or sum(1 for x in _details(allr, "always_human") if ENV_RE.match(x)))
                add("G1", f"The code reads {_plural(n, 'environment variable')} it did not read before, or stopped reading "
                          f"{'it' if n == 1 else 'them'} (names in Details).", ["always_human"])
                env_done = True
            continue
        if d.startswith("more than 100 changed files"):
            continue                                        # G4 below
        if d.startswith("security tool check failed: "):
            text = f"Security check {d.split(': ', 1)[1]} failed."
        elif d.startswith("dependency-review failed"):
            text = "Dependency review found a critical CVE."
        elif d.startswith("untrusted or fork author"):
            text = "The author is outside the repository's collaborators or works from a fork."
        elif d.startswith("label agentive-janitor:"):
            text = f"This is a janitor cleanup PR ({d.split('agentive-janitor:', 1)[1]}); a human merges it."
        elif d.startswith("agentive-check: tag"):
            text = "It adds or removes a tag that ties a written rule to the check enforcing it (a line starting `agentive-check:`)."
        elif d.startswith("review rules invalid"):
            text = f"The review rules in code_review.md did not validate, so the framework defaults were used ({d.split(': ', 1)[-1]})."
        else:
            text = d[0].upper() + d[1:] + ("" if d.endswith(".") else ".")
        later.append(text)
    if "large_change" in codes or any(d.startswith("more than 100 changed files") for d in _details(allr, "always_human")):
        ds = rv1.get("diff_stats") or {}
        lines = (ds.get("added") or 0) + (ds.get("deleted") or 0)
        add("G4", f"It is large: {ds.get('files', '?')} files, {lines:,} changed lines.", ["large_change"])
    for text in later:
        add("G12", text, ["always_human"])
    for d in _details(allr, "chain_approval"):
        p = d.replace("chain artifact ", "").replace(" added or modified", "")
        add("G12", f"It adds or changes a plan document ({p}); plan documents are approved by a human.", ["chain_approval"])
    # ---- G11 author, G8 findings, G7 checks, G10 policy
    if "bot_pr_exempt" in codes:
        add("G11", "A bot opened this PR, so a human reviews and merges it.", ["bot_pr_exempt"])
    if "human_pr_exempt" in codes:
        add("G11", "This PR has no agentive review record (not opened by an agent), so a human reviews and merges it.",
            ["human_pr_exempt"])
    for d in _details(reasons, "inconclusive_finding"):
        add("G8", f"The verifier could not confirm or refute finding {d}; a human decides.", ["inconclusive_finding"])
    for d in _details(reasons, "finding_disputed"):
        fid, _, ans = d.partition(" answered ")
        add("G8", f"The author answered finding {fid} as \"{ans}\"; a human checks that answer.", ["finding_disputed"])
    for d in _details(reasons, "review_rounds_exceeded"):
        add("G8", f"{d[0].upper()}{d[1:]}.", ["review_rounds_exceeded"])
    for d in _details(reasons, "pending_verbs"):
        m = re.search(r"required checks? (.+?) ha(?:s|ve) no command", d)
        verbs = m.group(1) if m else "a check"
        one = m and "," not in verbs
        add("G7", f"You required {verbs} in .agents/framework.yaml, but this repo has no command for {'it' if one else 'them'}.",
            ["pending_verbs"])
    if "no_required_check" in codes:
        add("G7", "No required check has a command, so nothing was verified.", ["no_required_check"])
    for d in _details(reasons, "gate_only"):
        for g in d.split(", "):
            add("G7", GATE_ONLY.get(g, g), ["gate_only"])
    if "auto_merge_off" in codes:
        add("G10", "Auto-merge is turned off for this repository (review.auto_merge: off).", ["auto_merge_off"])
    for d in _details(reasons, "score_above_threshold"):
        m = re.match(r"score (\S+) > (\S+)", d)
        add("G10", f"Risk score {m.group(1)} is above the auto-merge limit {m.group(2)}." if m else d, ["score_above_threshold"])
    if "rubric_invalid" in codes:
        add("G10", "The risk rubric override did not validate, so tier 1 goes to a human.", ["rubric_invalid"])
    if "tier1_off" in codes:
        add("G10", "The review config did not validate, so tier 1 goes to a human.", ["tier1_off"])
    for d in _details(reasons, "demoted"):
        add("G10", "Auto-merge is paused for this tier and author after a recent revert (open issue labeled "
                   "agentive:auto-merge-demoted).", ["demoted"])
    # ---- anything else: the detail sentence as is (never silently dropped)
    for c, d in _split(reasons):
        if c in used or c in HIDDEN or c.startswith("llm_raise:"):
            continue
        add("G12", (d or c), [c])
        used.add(c)
    return items


def verdict(report, items, extras=None):
    """(icon key, verdict words, what-to-do sentence) of the decision table (COMMENTUX R1, R2, R9)."""
    rv8 = (report or {}).get("rv8") or {}
    dec = rv8.get("decision")
    skip, fail = _skip(extras), _ai_failure(extras)
    codes = {c for it in items for c in it["codes"]}
    if dec == "pass":
        if rv8.get("auto_merge"):
            return "ok", "merges automatically", "Nothing to do. GitHub merges this PR once its merge requirements are met."
        return "ok", "ready for a human to merge", "Merge when you are satisfied."
    if dec == "block":
        first = next((it for it in items if it["block"]), None)
        c = (first or {}).get("codes", [""])[0]
        if c == "unanswered_finding":
            ids = re.search(r": (.+)\.$", first["text"]).group(1)
            n = len(ids.split(", "))
            return ("block", f"blocked — {_plural(n, 'AI finding')} {'needs' if n == 1 else 'need'} an answer",
                    f"Author: fix finding{'' if n == 1 else 's'} {ids} or reply in {'its' if n == 1 else 'each'} thread with an "
                    f"agentive-reply line, then push.")
        if c == "explainer_missing":
            return ("block", "blocked — PR body needs an Explainer",
                    "Author: add the ## Explainer section (run /pr --explain); editing the PR body re-runs the gate.")
        if c == "chain_order":
            return "block", "blocked — plan steps out of order", "Author: " + first["text"].split(": ", 1)[1]
        return ("block", "blocked — required check failed",
                "Author: fix the failed check (open its job log), then push.")
    if "would_auto_merge" in {c for c, _ in _split(rv8.get("reasons"))} and not [it for it in items if it["group"] != "G10"]:
        setting = rv8.get("auto_merge_setting") or cfgmod.AUTO_MERGE_DEFAULT
        return ("ok", "ready for a human to merge",
                f"Merge when you are satisfied. This PR would have merged automatically; auto-merge is in "
                f"{'shadow mode for tier 1' if setting == 'low_risk' else setting + ' mode'}, so it is labeled "
                f"`{cfgmod.AUTO_MERGE_LABEL}` instead.")
    if skip and skip["kind"] == "workflow_change":
        if _first_install(skip):
            return ("warn", "needs a human review — install PR",
                    "Review this install PR and merge it by hand. Auto-merge is off for this PR. The AI review could not "
                    "run on it; it runs on every PR opened after this one is merged.")
        return ("warn", "needs a human review — workflow change",
                "Review the workflow change and merge it by hand; PRs opened after it is merged get the AI review again.")
    if skip and skip["kind"] == "stale_merge":
        return ("warn", "needs a human review — AI review skipped",
                "Author: push a new commit, or close and reopen the PR (re-running this run does not help).")
    if skip:
        nxt = skip.get("next") or "a human reviews and merges"
        return "warn", "needs a human review — AI review skipped", nxt[:1].upper() + nxt[1:] + ("" if nxt.endswith(".") else ".")
    if fail:
        fix = llmstate_fix(fail[0], fail[2])
        return ("warn", "needs a human review — AI review failed",
                f"Maintainer: {fix}{f' ([run]({fail[1]}))' if fail[1] else ''}. Until it passes, a human reviews this PR.")
    if "ai_review_missing" in codes:
        return ("warn", "needs a human review — AI review failed",
                "Maintainer: open the failed ai-review job, fix the cause, and re-run it (or push a new commit).")
    if "dependency_change" in codes:
        from . import rv1 as rv1mod
        return ("warn", "needs a human review — dependency change",
                "Check the dependency change: " + rv1mod.DEPENDENCY_NEXT.replace("a human reviews the dependency change: ", "")
                .replace("; then merges", "; then merge") + ".")
    if codes & {"trust_boundary", "tier2_class"}:
        return ("warn", "needs a human review — security-sensitive change",
                "Check the sensitive files listed in Details and the AI findings, then merge by hand. Auto-merge never "
                "applies to this PR.")
    if codes & {"pending_verbs", "no_required_check"}:
        return ("warn", "needs a human review",
                "Maintainer: give the missing check a command (tell your agent, then run /agentive:update), or remove it from "
                "review.pipeline.stages.RV1.require_checks in .agents/framework.yaml. Until then a human reviews and merges.")
    if codes & {"inconclusive_finding", "finding_disputed", "review_rounds_exceeded"}:
        return ("warn", "needs a human review",
                "Check the AI findings below and the author's answers, then merge by hand.")
    return "warn", "needs a human review", "Review and merge by hand. Auto-merge is off for this PR."


def llmstate_fix(code, text=None):
    """The fix half of llmstate.SENTENCES (after the first '; '), or the whole sentence. text: the sentence as the failure text
    carries it (a no_secret sentence names the secrets to add)."""
    s = text or llmstate.sentence(code)
    return s.split("; ", 1)[1] if "; " in s else "open the ai-review job log, fix the cause, and re-run the job"


def checks_line(report):
    """`lint passed · test passed · typecheck not run (no command in this repo)` (COMMENTUX R8; E2E-34 display)."""
    rc = _required_state(report) or {}
    checks = [c for c in ((report or {}).get("rv1") or {}).get("checks") or [] if isinstance(c, dict)]
    st = {c.get("name"): c.get("status") for c in checks}
    names = rc.get("required") if rc else [c.get("name") for c in checks if c.get("name") in ("lint", "typecheck", "test", "build")]
    parts = [f"{v} {STATUS_WORD.get(st.get(v), 'not reported')}" for v in names or []]
    parts += [f"{v} not run (no command in this repo)" for v in rc.get("not_required") or []]
    return " · ".join(parts) or "none required"


def findings_line(report, extras=None):
    fnd = (report or {}).get("findings") or []
    imp = [f for f in fnd if f["severity"] == "important" and (f.get("verification") or {}).get("verdict") != "refuted"]
    refuted = [f for f in fnd if (f.get("verification") or {}).get("verdict") == "refuted"]
    codes = {c for c, _ in _split(((report or {}).get("rv8") or {}).get("reasons"))}
    if _skip(extras):
        return "not run (the AI review was skipped)"
    if _ai_failure(extras):
        return "not run (the AI review step failed)"
    if "ai_review_missing" in codes and not fnd:
        return "not reported (the AI review job left no report)"
    if not imp:
        return "none" if not refuted else f"none open ({len(refuted)} refuted by the verifier)"
    b = sum(1 for f in imp if f.get("blocking"))
    return f"{len(imp)} important{f', {b} blocking' if b else ''} (table below)"


def _why_block(items):
    """The visible Why lines (max 5: four and a `+ k more` line when there are more) and the overflow."""
    if len(items) <= WHY_MAX:
        return [f"- {it['text']}" for it in items], []
    keep = items[:WHY_MAX - 1]
    rest = items[WHY_MAX - 1:]
    return [f"- {it['text']}" for it in keep] + [f"- + {len(rest)} more in Details"], rest


def detail_lines(report, extras=None, reasons=None, overflow=(), notes=(), final=True):
    """The lines inside <details>: decision, classes, score, reason codes, named lists, required checks, auto-merge setting."""
    report, extras = report or {}, extras or {}
    rv8, rv4, rv3, rv7b = (report.get(k) or {} for k in ("rv8", "rv4", "rv3", "rv7b"))
    rv1, rv2 = report.get("rv1") or {}, report.get("rv2") or {}
    rnd = report.get("review_round") or {}
    reasons = list(reasons if reasons is not None else rv8.get("reasons") or [])
    out = []
    if rv8:
        concl = rv8.get("check_conclusion") or ("failure" if rv8.get("decision") == "block" else "success")
        # E2E-45: the ai-review job writes this comment before the gate has decided (agentive-independence and the other checks
        # come later), so its first version does not claim the gate's conclusion; the gate job rewrites the comment from its own
        # final decision (final=True)
        gate = (f"(required check `agentive-review-gate`: {concl})" if final else
                "so far; the `agentive-review-gate` job makes the final decision and updates this comment")
        out.append(f"- Decision `{rv8.get('decision')}` {gate} · tier {rv4.get('tier', '-')}"
                   + (f" · round {rnd.get('round', 1)} ({rnd.get('mode', 'full')})" if rnd else "")
                   + (f" · head `{str(report.get('head_sha') or '')[:12]}`" if report.get("head_sha") else ""))
    if rv3.get("classes"):
        method = {"llm": "rules, then the LLM classifier"}.get(rv3.get("method"), rv3.get("method") or "rules")
        out.append(f"- Change classes: {', '.join(rv3['classes'])} ({method})")
    if rv7b:
        if rv7b.get("score") is not None:
            hit = [i.get("id") for i in rv7b.get("items") or [] if i.get("hit")]
            out.append(f"- Risk score: {rv7b['score']} (limit {rv7b.get('human_if_score_gt', '-')})" + (f": {', '.join(hit)}" if hit else ""))
        elif rv7b.get("reason") != "not_tier1":
            out.append(f"- Risk score: not computed ({rv7b.get('reason', '-')})")
    codes = util.uniq([c for c, _ in _split(list(rv4.get("reasons") or []) + reasons)])
    if codes:
        out.append("- Reason codes: " + " ".join(f"`{c}`" for c in codes))
    hp = _hit_paths(rv2)
    if hp:
        out.append("- Sensitive paths by reason: " + " · ".join(
            f"{HIT_NAMES.get(r, r)} {len(ps)} ({', '.join(f'`{p}`' for p in ps[:5])}{', …' if len(ps) > 5 else ''})"
            for r, ps in sorted(hp.items(), key=lambda kv: (-len(kv[1]), kv[0]))))
    cs = rv2.get("confirm_scope_hits") or []
    if cs:
        out.append("- Confirmation needed for: " + " · ".join(f"{n} {k}" for n, k in _counts(cs, "category", SCOPE_NAMES)))
    env = (rv1.get("signals") or {}).get("env_vars_changed") or []
    if env:
        out.append(f"- Environment variables added or removed ({len(env)}): " + ", ".join(f"`{e}`" for e in env))
    dc = (rv1.get("signals") or {}).get("dependency_changes") or {}
    if "dependency_change" in codes:        # E2E-31: the reason sentence is in Why only; Details holds the full lists, no sentence
        parts = [f"{label} " + ", ".join(f"`{x}`" for x in items) for label, items in (
            ("added", dc.get("added") or []), ("changed", util.uniq(c["name"] for c in dc.get("changed") or [])),
            ("removed", dc.get("removed") or []), ("lockfiles", dc.get("lockfiles") or []),
            ("unreadable", dc.get("unreadable") or [])) if items]
        if parts:
            out.append("- Dependency change lists: " + " · ".join(parts))
    for it in overflow:
        out.append(f"- Also: {it['text']}")
    if (extras or {}).get("ai_step_failed"):
        fail = _ai_failure(extras)
        code = fail[0] if fail else "unclassified"
        out.append(f"- Error code: `ai_step_failed:{code}`."
                   + ("" if code in NO_RAW_HINT else " No error text is copied here: re-run the job with debug logging, or run the "
                      "same command locally with `claude -p`, to read it.")
                   + (f" Run: {fail[1]}" if fail and fail[1] else ""))
    skip = _skip(extras)
    if skip:
        out.append(f"- AI review skipped (`{skip['kind']}`): {skip.get('detail', '')}")
    if any(c == "coverage_unavailable" for c, _ in _split(reasons)):
        out.append("- Not checked: new-code coverage (no coverage report; never blocks).")
    rc = _required_state(report)
    if rc:
        src = rc.get("source", "framework default")
        src = ("this PR's agentive manifest; a first install, the base has none" if src == cfgmod.FIRST_INSTALL_SOURCE
               else src)
        line = f"- Required checks: {', '.join(rc.get('required') or []) or 'none'} (from {src})."
        if rc.get("not_required"):
            nr = rc["not_required"]
            line += f" {', '.join(nr)} {'has' if len(nr) == 1 else 'have'} no command in this repo, so {'it is' if len(nr) == 1 else 'they are'} not required."
        if rc.get("source") == cfgmod.FIRST_INSTALL_SOURCE:
            line += " Shown for information: the gate read the base branch, which has no agentive manifest yet."
        out.append(line)
    if rv8:
        setting = rv8.get("auto_merge_setting") or cfgmod.AUTO_MERGE_DEFAULT
        dflt = " (framework default)" if setting == cfgmod.AUTO_MERGE_DEFAULT else ""
        out.append(f"- Auto-merge setting `{setting}`{dflt}: {cfgmod.AUTO_MERGE_MEANING.get(setting, 'unknown value')}. To change it, "
                   "set `review.auto_merge` in `.agents/framework.yaml` (off | dry_run | low_risk | low_and_medium_risk) or tell your "
                   "agent; merging also needs the repository setting \"Allow auto-merge\".")
    for p in notes:
        out.append(f"- Note: {p}")
    return out


def _details_summary(report, extras):
    named = []
    if _hit_paths((report or {}).get("rv2")):
        named.append("sensitive paths")
    if (((report or {}).get("rv1") or {}).get("signals") or {}).get("env_vars_changed"):
        named.append("environment variables")
    return "Details: decision, reason codes, " + "".join(n + ", " for n in named) + "auto-merge setting"


def summary_comment(report, posted_now, extras, final=True):
    """The one sticky agentive-review-summary comment (COMMENTUX): marker, verdict heading, What to do, Why (only when a human
    acts), Checks, AI findings, the findings table, folded minor notes and folded Details. posted_now is kept for the call
    shape (the count is in the job log). final=False (the ai-review job, before the gate) leaves the gate's conclusion out
    (E2E-45); the gate job posts the same comment again with final=True from its own decision."""
    extras = extras or {}
    rv4 = report.get("rv4") or {}
    fnd = report.get("findings") or []
    imp = [f for f in fnd if f["severity"] == "important" and (f.get("verification") or {}).get("verdict") != "refuted"]
    minor = [f for f in fnd if f["severity"] in ("nit", "pre_existing") and (f.get("verification") or {}).get("verdict") != "refuted"]
    reasons, notes = skipped_view((report.get("rv8") or {}).get("reasons"), extras.get("llm_problems"),
                                  extras.get("ai_review_skipped"), rv4.get("reasons"))
    shown = {r[len("llm_raise: "):] for r in reasons if r.startswith("llm_raise: ")}
    notes = [p for p in notes if p not in shown]                  # E2E-26: a problem shown as a reason is not repeated as a note
    items = why_items(report, extras, reasons)
    icon, words, todo = verdict(report, items, extras)
    out = [SUMMARY_MARKER, f"### {ICON[icon]} agentive review: {words}", "", f"**What to do:** {todo}", ""]
    overflow = []
    if (report.get("rv8") or {}).get("decision") != "pass" and items:
        why, overflow = _why_block(items)
        out += ["**Why**"] + why + [""]
    out.append(f"**Checks:** {checks_line(report)}")
    out.append(f"**AI findings:** {findings_line(report, extras)}")
    if extras.get("reviewer_eval"):                                # E2E-25: the reviewer-eval comment cannot score this change
        out.append(f"**Reviewer eval:** {extras['reviewer_eval']}")
    if imp:
        out += ["", "| id | file:line | verifier | answer |", "|---|---|---|---|"]
        for f in imp:
            r = f.get("response") or {}
            out.append(f"| {f['id']} | `{loc(f)}` | {(f.get('verification') or {}).get('verdict', '-')} | "
                       f"{r.get('reason', 'open')}{' (' + r['source'] + ')' if r.get('source') else ''} |")
    if minor:
        out += ["", f"<details><summary>{_plural(len(minor), 'minor note')} — not blocking, no action needed</summary>", ""]
        for f in minor:
            out.append(f"- [{f['severity']}] `{loc(f)}` {f['summary']}")
        out += ["", "</details>"]
    out += ["", f"<details><summary>{_details_summary(report, extras)}</summary>", ""]
    out += detail_lines(report, extras, reasons, overflow, notes, final)
    out += ["", "</details>"]
    return "\n".join(out) + "\n"


def gate_summary(report, extras=None):
    """The markdown the gate writes to $GITHUB_STEP_SUMMARY (COMMENTUX section 7): the same verdict, What to do and Why lines as
    the summary comment; the job log keeps its plain lines and the AGENTIVE_GATE= line (prstate reads that one)."""
    extras = extras or {}
    rv4 = report.get("rv4") or {}
    reasons, notes = skipped_view((report.get("rv8") or {}).get("reasons"), extras.get("llm_problems"),
                                  extras.get("ai_review_skipped"), rv4.get("reasons"))
    items = why_items(report, extras, reasons)
    icon, words, todo = verdict(report, items, extras)
    out = [f"### {ICON[icon]} agentive-review-gate: {words} (tier {rv4.get('tier', '-')})", "", f"**What to do:** {todo}", ""]
    overflow = []
    if (report.get("rv8") or {}).get("decision") != "pass" and items:
        why, overflow = _why_block(items)
        out += why + [""]
    out += [f"**Checks:** {checks_line(report)}", "", "<details><summary>Reason codes and auto-merge setting</summary>", ""]
    out += detail_lines(report, extras, reasons, overflow, notes)
    out += ["", "</details>"]
    return "\n".join(out) + "\n"
