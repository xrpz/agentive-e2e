# agentive-unit: lib.agentive
# design: spec.review_pipeline.reviewer_eval, spec.review_pipeline.reviewer_eval.scoring, spec.review_pipeline.reviewer_eval.ci, spec.review_pipeline.reviewer_eval.reviewer_paths, contracts[reviewer-eval-case], decisions.accepted[NOBASELINE], decisions.accepted[LOCALTIER], decisions.accepted[COMMENTUX], decisions.accepted[EVALBUNDLE] (owner D)
"""Reviewer eval: which PRs are a reviewer change, case files, scoring and the summary block. No model calls here:
the runner (.agents/bin/agentive-reviewer-eval run) starts the headless runs; this module is deterministic.

Scoring (design): counted = findings the pipeline would post (after RV7 and RV6) with severity important, on a line;
a counted finding catches an expected range when its file is equal and its line or one of its instances[].line is in
[line_from - slack, line_to + slack] (EVALBUNDLE: slack = the range's own `slack`, else DEFAULT_SLACK = 2 lines [design judgment]);
a forbidden range (negative case) has slack 0. Positive pass = every expected range caught; negative pass = no blocking
finding and no counted finding in a forbidden range. Diagnostic only (never in pass or adopt): a positive run that missed
a range but posted a counted finding on the same file elsewhere (bug_class equal when the case has one) is `wrong place`. Each side runs N times; a case's result is its majority; side counts are means.
A failed run (no report) counts as a failed run of that case [design judgment].
No baseline (E2E-16, NOBASELINE): when the base commit has no .agents/bin/agentive-review (the first install) the
baseline side cannot run at all, so nothing is compared: reason no_baseline, adopt_suggested true, no eval-override.
eval-override (E2E-25, EVALOVERRIDE): the PR-body line `eval-override: <reason>` is a rule for the human who merges; no check
reads it (not this module, not the gate), so the comment says so and names what to do instead of implying a block.
"""
import os
import re

from .. import ayaml
from . import config as cfgmod
from . import gitio
from . import util

REVIEWER_GLOBS = [".agents/agents/**", ".claude/agents/**", ".codex/agents/**", ".agents/bin/agentive-review",
                  ".agents/lib/agentive/review/**", "code_review.md", "REVIEW.md"]
CASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
DEFAULT_SLACK = 2          # EVALBUNDLE [design judgment]: lines of tolerance around an expected range; no official source gives a value (unverified)
MAX_SLACK = 20             # a per-range `slack` above this is a schema error: a wider window is a different range
MIN_POSITIVES = 10         # below this the comparison is marked "reference only" (EVALBUNDLE [design judgment]; Anthropic suggests starting with 20-50)
BUG_CLASSES = ("logic", "error-handling", "concurrency", "data-contract", "security", "test-gap", "spec-drift", "other")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DATE10 = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CASE_KEYS = {"id", "pr", "label", "expected_findings", "budget_usd", "origin", "forbidden_findings",
             "base_sha", "head_sha", "bug_class", "note", "source", "created", "drafted_by"}   # the last seven: optional, EVALBUNDLE
MARKER = "<!-- agentive-reviewer-eval -->"
BASELINE_BIN = ".agents/bin/agentive-review"     # the program the baseline run calls (agentive-reviewer-eval run)
CASES_HELP = "add one positive and one negative case under evals/reviewer/ (evals/reviewer/README.md)"
# E2E-25: what the eval-override line is (EVALOVERRIDE: a PR-body line for the human merge; CI and the gate never parse it)
OVERRIDE_FACT = "a rule for the human who merges: no check reads the line, and the gate does not wait for it"


# ---------------------------------------------------------------- reviewer change (reviewer_paths)
def _job_block(text, job="ai-review"):
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^  " + re.escape(job) + r":\s*(#.*)?$", ln):
            out = [ln]
            for ln2 in lines[i + 1:]:
                if ln2.strip() and not ln2.startswith("   "):
                    break
                out.append(ln2)
            return "\n".join(out).rstrip()
    return None


def reviewer_change(repo, base, head):
    """(bool, [reasons]) per review_pipeline.reviewer_eval.reviewer_paths (the library of the pipeline script is
    counted with the script [design judgment])."""
    d = gitio.Diff(repo, base, head)
    reasons = []
    for st, p, old in d.name_status():
        for n in [p] + ([old] if old else []):
            g = util.first_match(REVIEWER_GLOBS, n)
            if g:
                reasons.append(f"{n} ({g})")
                break
        if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")):
            a, b = _job_block(gitio.show(repo, base, old or p)), _job_block(gitio.show(repo, head, p))
            if a != b and (a or b):
                reasons.append(f"{p}: ai-review job changed")
        if p == ".agents/framework.yaml":
            a = cfgmod.pipeline_section(lambda x: gitio.show(repo, base, x))
            b = cfgmod.pipeline_section(lambda x: gitio.show(repo, head, x))
            if a != b:
                reasons.append(".agents/framework.yaml review.pipeline changed")
    return bool(reasons), util.uniq(reasons)


def has_baseline(repo, base):
    """False when the base commit has no agentive reviewer to run (E2E-16): .agents/bin/agentive-review is missing there.
    Reviewer files the repository had before agentive (.claude/agents/, REVIEW.md, code_review.md) do not make a baseline,
    because the baseline run executes that program [design judgment]."""
    return gitio.exists(repo, base, BASELINE_BIN)


# ---------------------------------------------------------------- cases
def validate_case(doc, file_id):
    p = []
    if not isinstance(doc, dict):
        return ["case must be a mapping"]
    extra = set(doc) - CASE_KEYS
    if extra:
        p.append(f"unknown keys {sorted(extra)}")
    if doc.get("id") != file_id or not CASE_ID.match(str(doc.get("id", ""))):
        p.append(f"id must equal the file name ({file_id})")
    if not isinstance(doc.get("pr"), (str, int)) or not str(doc.get("pr")).strip():
        p.append("pr is required (#N or base..head)")
    lab = doc.get("label")
    if lab not in ("positive", "negative"):
        p.append("label must be positive or negative")

    def ranges(key):
        v = doc.get(key)
        if not isinstance(v, list) or not v:
            return [f"{key} must be a non-empty list"]
        q = []
        allowed = {"file", "line_from", "line_to"} | ({"slack"} if key == "expected_findings" else set())   # forbidden ranges: slack 0, no override
        for r in v:
            if not (isinstance(r, dict) and isinstance(r.get("file"), str) and r["file"] and _int(r.get("line_from"))
                    and _int(r.get("line_to")) and 1 <= r["line_from"] <= r["line_to"] and set(r) <= allowed):
                q.append(f"{key} entry {r!r} needs file, line_from <= line_to" + (" (and optionally slack)" if key == "expected_findings" else ""))
            elif "slack" in r and not (_int(r["slack"]) and 0 <= r["slack"] <= MAX_SLACK):
                q.append(f"{key} entry {r!r}: slack must be an integer 0..{MAX_SLACK}")
        return q
    if lab == "positive":
        p += ranges("expected_findings")
        o = doc.get("origin")
        if not (isinstance(o, dict) and o.get("kind") in ("fix_pr", "revert", "reproduction_diff", "injected_bug") and o.get("ref")):
            p.append("origin {kind: fix_pr | revert | reproduction_diff | injected_bug, ref} is required on a positive case")
        if "forbidden_findings" in doc:
            p.append("forbidden_findings is for negative cases only")
    elif lab == "negative":
        if "origin" in doc or "expected_findings" in doc:
            p.append("origin and expected_findings are for positive cases only")
        if "forbidden_findings" in doc:
            p += ranges("forbidden_findings")
    if "budget_usd" in doc and not (isinstance(doc["budget_usd"], (int, float)) and doc["budget_usd"] > 0):
        p.append("budget_usd must be > 0")
    p += _optional_fields(doc)
    return p


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _optional_fields(doc):
    """EVALBUNDLE: the optional case fields (base_sha, head_sha, bug_class, note, source, created, drafted_by). None of them is
    scored except bug_class (the wrong-place diagnostic) and the shas (which commits the runner uses)."""
    p = []
    shas = [k for k in ("base_sha", "head_sha") if k in doc]
    for k in shas:
        if not (isinstance(doc[k], str) and SHA40.match(doc[k])):
            p.append(f"{k} must be a 40-character lowercase hex sha")
    if len(shas) == 1:
        p.append("base_sha and head_sha go together")
    if "bug_class" in doc and doc["bug_class"] not in BUG_CLASSES:
        p.append(f"bug_class must be one of {', '.join(BUG_CLASSES)}")
    if "note" in doc and not (isinstance(doc["note"], str) and doc["note"].strip() and len(doc["note"]) <= 300):
        p.append("note must be a non-empty string of at most 300 characters")
    if "created" in doc and not (isinstance(doc["created"], str) and DATE10.match(doc["created"])):
        p.append("created must be a date YYYY-MM-DD")
    if "drafted_by" in doc and doc["drafted_by"] not in ("distill", "human"):
        p.append("drafted_by must be distill or human")
    if "source" in doc:
        src = doc["source"]
        if not (isinstance(src, dict) and src and set(src) <= {"finding_id", "reply", "range_basis"}):
            p.append("source needs at least one of finding_id, reply, range_basis (and no other key)")
        else:
            if "finding_id" in src and not (isinstance(src["finding_id"], str) and src["finding_id"]):
                p.append("source.finding_id must be a non-empty string")
            if "reply" in src and src["reply"] not in ("wrong", "intentional", "not_important"):
                p.append("source.reply must be wrong, intentional or not_important")
            if "range_basis" in src and src["range_basis"] not in ("proposed", "confirmed"):
                p.append("source.range_basis must be proposed or confirmed")
    return p


def load_cases(case_dir):
    """([case], [problem]) from evals/reviewer/<id>.yaml."""
    cases, probs = [], []
    if not os.path.isdir(case_dir):
        return cases, probs
    for name in sorted(os.listdir(case_dir)):
        if not name.endswith(".yaml"):
            continue
        fid = name[:-5]
        try:
            doc = ayaml.load(os.path.join(case_dir, name))
        except (ayaml.YAMLError, OSError) as e:
            probs.append(f"{name}: {e}")
            continue
        p = validate_case(doc, fid)
        if p:
            probs += [f"{name}: {x}" for x in p]
            continue
        doc["pr"] = str(doc["pr"])
        cases.append(doc)
    return cases, probs


# ---------------------------------------------------------------- scoring
def counted(report):
    """Findings of a review report that the pipeline would post: important, on a line, not refuted."""
    out = []
    for f in (report or {}).get("findings") or []:
        if f.get("severity") != "important" or (f.get("verification") or {}).get("verdict") == "refuted":
            continue
        out.append({"file": f["file"], "line": f["line"], "instances": f.get("instances") or [], "blocking": bool(f.get("blocking")),
                    "bug_class": f.get("bug_class")})
    return out


def spots(finding):
    """[(file, line)] of a counted finding: its own line and every instances[].line."""
    return [(finding["file"], finding["line"])] + [(i.get("file", finding["file"]), i["line"]) for i in finding.get("instances") or []]


def range_slack(rng):
    """Lines of tolerance of an EXPECTED range: its own `slack`, else DEFAULT_SLACK (forbidden ranges always use 0)."""
    return rng.get("slack", DEFAULT_SLACK)


def catches(finding, rng, slack=0):
    """A counted finding catches a range when its file is equal and its line or one of its instances[].line lies in
    [line_from - slack, line_to + slack]. slack defaults to 0 here: the caller passes range_slack(rng) for expected ranges."""
    lo, hi = rng["line_from"] - slack, rng["line_to"] + slack
    return any(f == rng["file"] and lo <= ln <= hi for f, ln in spots(finding))


def run_result(case, run):
    """(passed, unwanted) of one run: run = counted findings, or None for a failed run."""
    if run is None:
        return False, 1 if case["label"] == "negative" else 0
    if case["label"] == "positive":
        ok = all(any(catches(f, r, range_slack(r)) for f in run) for r in case["expected_findings"])
        return ok, 0
    bad = [f for f in run if f["blocking"] or any(catches(f, r) for r in case.get("forbidden_findings") or [])]   # negatives: slack 0
    return not bad, len(bad)


def run_wrong_place(case, run):
    """Diagnostic only (EVALBUNDLE; never in pass, newly_* or adopt): a positive run that missed an expected range but posted a
    counted finding on the same file elsewhere. When the case has a bug_class the finding's bug_class must be equal. The code
    cannot tell the same bug on the wrong line from another problem, so a human reads it (the comment says so)."""
    if run is None or case["label"] != "positive":
        return False
    cls = case.get("bug_class")
    for r in case["expected_findings"]:
        if any(catches(f, r, range_slack(r)) for f in run):
            continue
        for f in run:
            if (not cls or f.get("bug_class") == cls) and any(file == r["file"] for file, _ in spots(f)):
                return True
    return False


def _side(cases, runs_by_case, n):
    per_run_caught, per_run_unwanted, results, wrong = [0] * n, [0] * n, {}, {}
    for c in cases:
        runs = list(runs_by_case.get(c["id"]) or [])
        runs += [None] * (n - len(runs))
        passes = 0
        wrong[c["id"]] = 0
        for i, r in enumerate(runs[:n]):
            ok, unwanted = run_result(c, r)
            passes += 1 if ok else 0
            if c["label"] == "positive" and ok:
                per_run_caught[i] += 1
            if c["label"] == "negative":
                per_run_unwanted[i] += unwanted
            if not ok and run_wrong_place(c, r):
                wrong[c["id"]] += 1
        results[c["id"]] = passes
    return per_run_caught, per_run_unwanted, results, wrong


NOT_RUN_CAUSES = ("fork", "no_secret")      # E2E-42: why the reviewer could not run (absent: unknown)


def summarize(cases, baseline_runs, candidate_runs, n, baseline_ref, candidate_ref, not_run=False, no_baseline=False, not_run_cause=None):
    """reviewer-eval-case.schema.json $defs.summary. Order: not_run, no_baseline, insufficient_cases, newly_missed, ...
    wrong_place (optional) is a diagnostic: it takes no part in the order or in adopt_suggested."""
    pos = [c for c in cases if c["label"] == "positive"]
    neg = [c for c in cases if c["label"] == "negative"]
    b_caught, b_unw, b_res, b_wp = _side(cases, baseline_runs, n)
    c_caught, c_unw, c_res, c_wp = _side(cases, candidate_runs, n)
    maj = n // 2 + 1

    def mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else 0.0
    summary = {
        "baseline_ref": baseline_ref, "candidate_ref": candidate_ref, "n_runs": n,
        "cases": [{"id": c["id"], "label": c["label"], "baseline_pass_runs": b_res[c["id"]], "candidate_pass_runs": c_res[c["id"]]} for c in cases],
        "positives": {"total": len(pos), "baseline_mean": mean(b_caught), "candidate_mean": mean(c_caught),
                      "baseline_spread": (max(b_caught) - min(b_caught)) if b_caught else 0},
        "negatives": {"total": len(neg), "baseline_mean": mean(b_unw), "candidate_mean": mean(c_unw),
                      "baseline_spread": (max(b_unw) - min(b_unw)) if b_unw else 0},
        "newly_caught": sorted(c["id"] for c in pos if b_res[c["id"]] < maj <= c_res[c["id"]]),
        "newly_missed": sorted(c["id"] for c in pos if c_res[c["id"]] < maj <= b_res[c["id"]]),
        "newly_noisy": sorted(c["id"] for c in neg if c_res[c["id"]] < maj <= b_res[c["id"]]),
    }
    wp = [{"id": c["id"], "baseline_runs": b_wp[c["id"]], "candidate_runs": c_wp[c["id"]]} for c in pos if b_wp[c["id"]] or c_wp[c["id"]]]
    if wp:      # optional, diagnostic only (EVALBUNDLE); absent when no run posted a finding on the right file at another line
        summary["wrong_place"] = wp
    if not_run:
        summary.update(adopt_suggested=False, reason="not_run")
        if not_run_cause in NOT_RUN_CAUSES:      # E2E-42: a same-repository PR without the secret is not a fork PR
            summary["not_run_cause"] = not_run_cause
    elif no_baseline:      # nothing to adopt over: the human merge of the tier 2 install PR accepts the first reviewer
        summary.update(adopt_suggested=True, reason="no_baseline")
    elif not pos or not neg:
        summary.update(adopt_suggested=False, reason="insufficient_cases")
    elif summary["newly_missed"]:
        summary.update(adopt_suggested=False, reason="newly_missed")
    else:
        p, q = summary["positives"], summary["negatives"]
        improved = None
        if p["candidate_mean"] != p["baseline_mean"]:
            improved = p["candidate_mean"] - p["baseline_mean"] > p["baseline_spread"]
        elif q["candidate_mean"] != q["baseline_mean"]:
            improved = q["baseline_mean"] - q["candidate_mean"] > q["baseline_spread"]
        if improved:
            summary.update(adopt_suggested=True, reason="improved")
        elif not summary["newly_noisy"]:
            summary.update(adopt_suggested=True, reason="no_regression")
        else:
            summary.update(adopt_suggested=False, reason="no_gain")
    return summary


# COMMENTUX (decisions.accepted[COMMENTUX], 2026-11-01): the reviewer-eval comment says first what it is and what the human
# does; the file list and the machine-read YAML are folded [design judgment: wording and group names]
PURPOSE = "CI re-runs the reviewer on saved test cases before and after the change and compares the results."
GROUPS = ((".agents/bin/agentive-review", "review program"), (".agents/lib/agentive/review/**", "review program"),
          (".agents/agents/**", "agents"), (".claude/agents/**", "Claude Code agents"), (".codex/agents/**", "Codex agents"),
          ("code_review.md", "review rules"), ("REVIEW.md", "review rules"))
HUMAN_OVERRIDE = ("To merge it, add one line `eval-override: <reason>` to the PR body (for example `eval-override: no eval cases yet; "
                  "reviewed by hand`). No check reads that line; it records why for later readers.")
VERDICTS = {
    "no_baseline": ("info", "not compared — first install"),
    "not_run": ("info", "not run"),                    # E2E-42: NOT_RUN_WORDS says why
    "not_run:fork": ("info", "not run — fork PR"),
    "not_run:no_secret": ("info", "not run — no AI credential secret"),
    "insufficient_cases": ("warn", "not scored — review this reviewer change by hand"),
    "improved": ("ok", "improved — merge as usual"),
    "no_regression": ("ok", "no regression — merge as usual"),
    "no_gain": ("warn", "no gain"),
}
ICONS = {"ok": "✅", "warn": "⚠️", "info": "ℹ️"}     # no block icon: the reviewer eval blocks no check


def reviewer_groups(reasons):
    """[(group name, [path or reason])] of the reviewer_change reasons (`<path> (<glob>)`, `<workflow>: ai-review job changed`,
    `.agents/framework.yaml review.pipeline changed`), in a fixed group order."""
    out = {}
    for r in reasons or []:
        if r.endswith(": ai-review job changed"):
            name, item = "ai-review workflow job", "`" + r[:-len(": ai-review job changed")] + "` (job `ai-review` changed)"
        elif r.startswith(".agents/framework.yaml review.pipeline"):
            name, item = "pipeline config", "`.agents/framework.yaml` (`review.pipeline` changed)"
        else:
            path, _, glob = r.rpartition(" (")
            path, glob = (path, glob.rstrip(")")) if path else (r, "")
            name = next((n for g, n in GROUPS if g == glob), None) or next((n for g, n in GROUPS if util.first_match([g], path)), "other")
            item = path
        out.setdefault(name, []).append(item)
    order = ["review program", "agents", "Claude Code agents", "Codex agents", "review rules", "ai-review workflow job",
             "pipeline config", "other"]
    return [(n, out[n]) for n in sorted(out, key=lambda n: (-len(out[n]), order.index(n) if n in order else 99))]


def _group_text(groups):
    """E2E-38: `name count, name count`; one group alone is just its name (its count is the file count before it)."""
    if len(groups) == 1:
        return groups[0][0]
    return ", ".join(f"{g} {len(v)}" for g, v in groups)


def _file_list(items):
    """`dir/`: `a`, `b` per directory (one path alone stays whole); an item that already holds backticks is kept as is."""
    out, dirs = [], {}
    for x in items:
        if "`" in x:
            out.append(x)
            continue
        d, _, b = x.rpartition("/")
        dirs.setdefault(d, []).append(b)
    for d, names in dirs.items():
        if len(names) == 1 or not d:
            out += [f"`{d + '/' if d else ''}{n}`" for n in names]
        else:
            out.append(f"`{d}/`: " + ", ".join(f"`{n}`" for n in names))
    return "; ".join(out)


def _result(s, secrets=None):
    """(Result sentence, What to do sentence, extra line or None) per reason."""
    reason = s["reason"]
    if reason == "no_baseline":
        return ("Nothing to compare: the base branch has no agentive reviewer yet, so no run was made.",
                "Nothing for this check. Merging this install PR accepts the first reviewer; no `eval-override` line is needed.",
                f"**To get a score next time:** {CASES_HELP}.")
    if reason == "not_run":
        cause = s.get("not_run_cause")
        if cause == "fork":
            return ("A fork PR gets no repository secrets, so the reviewer could not run.",
                    "Review this reviewer change by hand. " + HUMAN_OVERRIDE, None)
        if cause == "no_secret":      # E2E-42 / E2E-10b codes: the same fix as ai_step_failed:no_secret
            names = " or ".join(secrets or []) or "AI review credential"
            return (f"This repository has no AI review credential secret, so the reviewer could not run (this is not a fork PR).",
                    f"Maintainer: add the {names} repository secret (Settings > Secrets and variables > Actions), then push or re-run "
                    "this job to get a score. Until then, review this reviewer change by hand. " + HUMAN_OVERRIDE, None)
        return ("The reviewer could not run: the job had no repository secret to run it with.",
                "Review this reviewer change by hand. " + HUMAN_OVERRIDE, None)
    if reason == "insufficient_cases":
        why = ("`evals/reviewer/` has no case yet, so nothing was compared." if not s["cases"] else
               "`evals/reviewer/` needs a positive and a negative case, so the result is not compared.")
        return ("Not scored: " + why, "Review this reviewer change by hand. " + HUMAN_OVERRIDE,
                f"**To get a score next time:** {CASES_HELP}.")
    p, q = s["positives"], s["negatives"]
    if reason == "newly_missed":
        ids = ", ".join(s["newly_missed"])
        return (f"After the change the reviewer missed {ids}, which it caught before.",
                f"Author: change the reviewer so {ids} {'is' if len(s['newly_missed']) == 1 else 'are'} caught again. Or, to merge anyway, "
                "add `eval-override: <reason>` to the PR body (no check reads that line).", None)
    if reason == "no_gain":
        noisy = ", ".join(s["newly_noisy"]) or "a clean case"
        return (f"No improvement beyond run-to-run noise, and {noisy} became noisy.",
                "Author: reduce the new unwanted findings. Or, to merge anyway, add `eval-override: <reason>` to the "
                "PR body (no check reads that line).", None)
    if reason == "improved":
        return (f"After the change the reviewer caught more known bugs (or made fewer unwanted findings) than run-to-run noise "
                f"(known bugs caught {p['candidate_mean']} of {p['total']}, before {p['baseline_mean']}; spread {p['baseline_spread']}).",
                "Merge as usual (this PR still needs your review because it changes the reviewer).", None)
    return ("Same results before and after; no case got worse.",
            "Merge as usual (this PR still needs your review because it changes the reviewer).", None)


def _spot(file, a, b=None):
    return f"`{file}:{a}`" if b is None or b == a else f"`{file}:{a}-{b}`"


def expected_text(case):
    """The expected location of a case for the comment: `file:from-to` per expected range, or `clean` (negative; with the forbidden
    ranges when it has some). `-` when the comment was built without the case files."""
    if not case:
        return "-"
    if case["label"] == "positive":
        return ", ".join(_spot(r["file"], r["line_from"], r["line_to"]) for r in case["expected_findings"])
    forb = case.get("forbidden_findings") or []
    return "clean" + (", nothing at " + ", ".join(_spot(r["file"], r["line_from"], r["line_to"]) for r in forb) if forb else "")


def case_change(c, n):
    """(kind, text) of a case whose pass count differs between the sides, else None. kind is worse | better (regressions are listed
    first); the text says it in words so that color is never the only carrier (a majority crossing is newly missed / caught / noisy)."""
    b, a, maj = c["baseline_pass_runs"], c["candidate_pass_runs"], n // 2 + 1
    if a == b:
        return None
    pos = c["label"] == "positive"
    if a < b:
        crossed = a < maj <= b
        return "worse", ("worse: newly missed" if crossed else "worse: caught less often") if pos else \
            ("worse: newly noisy" if crossed else "worse: noisier")
    crossed = b < maj <= a
    return "better", ("better: newly caught" if crossed else "better: caught more often") if pos else \
        ("better: newly clean" if crossed else "better: less noisy")


def _run_text(case, run):
    """One run of a case in words: caught | missed (posted `f:l`) | missed (no finding) | clean | noisy (posted ...) | failed (no report)."""
    if run is None:
        return "failed (no report)"
    ok, _ = run_result(case, run)
    posted = ", ".join(f"`{f['file']}:{f['line']}`" for f in run[:3]) + (f" and {len(run) - 3} more" if len(run) > 3 else "")
    if case["label"] == "positive":
        if ok:
            return "caught"
        if not run:
            return "missed (no finding)"
        return ("missed at another line of the same file (posted " if run_wrong_place(case, run) else "missed (posted ") + posted + ")"
    return "clean" if ok else f"noisy (posted {posted})"


def _runs_text(case, runs, n):
    runs = list(runs or [])
    runs += [None] * (n - len(runs))
    return ", ".join(_run_text(case, r) for r in runs[:n])


def _case_rows(s, detail, with_wrong):
    """[(case_result, case or None, change or None, wrong-place text)] for every case of the summary, in summary order."""
    by_id = {c["id"]: c for c in (detail or {}).get("cases") or []}
    wp = {w["id"]: w for w in s.get("wrong_place") or []}
    n = s["n_runs"]
    out = []
    for c in s["cases"]:
        w = wp.get(c["id"])
        out.append((c, by_id.get(c["id"]), case_change(c, n), f"{w['baseline_runs']}/{n} → {w['candidate_runs']}/{n}" if w else "0"))
    return out


def _table(rows, n, with_wrong):
    head = "| case | expected | before | after | change |" + (" wrong place before → after |" if with_wrong else "")
    lines = [head, "|---|---|---|---|---|" + ("---|" if with_wrong else "")]
    for c, case, ch, wtxt in rows:
        text = ch[1] if ch else "same"
        lines.append(f"| {c['id']} | {expected_text(case)} | {c['baseline_pass_runs']}/{n} | {c['candidate_pass_runs']}/{n} | {text} |"
                     + (f" {wtxt} |" if with_wrong else ""))
    return lines


def _changed_section(s, detail):
    """(visible lines, folded lines): the changed-case table with regressions first, and the per-run evidence of those cases."""
    n = s["n_runs"]
    with_wrong = bool(s.get("wrong_place"))
    rows = _case_rows(s, detail, with_wrong)
    changed = sorted((r for r in rows if r[2]), key=lambda r: (r[2][0] != "worse", r[0]["id"]))
    total = len(rows)
    if not changed:
        return [f"**Changed cases (0 of {total}):** none; every case has the same result before and after."], []
    vis = [f"**Changed cases ({len(changed)} of {total})**, regressions first; the change column is text, not color", ""]
    vis += _table(changed, n, with_wrong)
    if not (detail or {}).get("cases"):
        return vis, []
    fold = ["<details><summary>Why: runs of the changed cases</summary>", ""]
    for c, case, ch, _ in changed:
        if not case:
            continue
        fold += [f"- {c['id']}, expected {expected_text(case)}",
                 f"  - before: {_runs_text(case, (detail.get('baseline') or {}).get(c['id']), n)}",
                 f"  - after: {_runs_text(case, (detail.get('candidate') or {}).get(c['id']), n)}"]
    fold += ["", "</details>"]
    return vis, fold


def comment(summary, reasons, notes=(), detail=None, secrets=None):
    """The one sticky agentive-reviewer-eval comment (COMMENTUX, EVALBUNDLE): marker, verdict heading, what the comment is, Result,
    What to do, the score table only for a real comparison, then outside the fold one noise sentence, a reference-only line while
    fewer than MIN_POSITIVES positive cases exist and the changed-case table (regressions first); folded: the per-run evidence of
    the changed cases, the per-case table, the reviewer files and the machine-read YAML block (util.fenced_blocks reads it anywhere;
    blank lines around the fence). detail = {cases: [case], baseline: runs_by_case, candidate: runs_by_case} is what the runner
    knows beyond the summary (expected locations and per-run results); without it the table shows `-` for the location.
    secrets: the credential secret names to add when summary.not_run_cause is no_secret (E2E-42)."""
    s = summary
    key = s["reason"]
    if key == "newly_missed":
        n = len(s["newly_missed"])
        icon, words = "warn", f"worse — {n} known bug{'s' if n != 1 else ''} no longer caught"
    else:
        icon, words = VERDICTS.get(f"{key}:{s.get('not_run_cause')}") or VERDICTS.get(key, ("warn", key.replace("_", " ")))
    groups = reviewer_groups(reasons)
    n_files = sum(len(v) for _, v in groups)
    gtext = _group_text(groups)
    result, todo, extra = _result(s, secrets)
    lines = [MARKER, f"### {ICONS[icon]} agentive reviewer eval: {words}", "",
             f"This PR changes the AI reviewer ({n_files} {'change' if n_files == 1 else 'changes'}: {gtext or '-'}). {PURPOSE}", "",
             f"**Result:** {result}", f"**What to do:** {todo}"]
    if extra:
        lines.append(extra)
    real = key not in ("no_baseline", "not_run", "insufficient_cases")
    fold_why = []
    if real:
        p, q = s["positives"], s["negatives"]
        lines += ["", f"| | before (`{s['baseline_ref'][:12]}`) | after (`{s['candidate_ref'][:12]}`) |", "|---|---|---|",
                  f"| known bugs caught (mean of {s['n_runs']} runs, of {p['total']} cases) | {p['baseline_mean']} "
                  f"(spread {p['baseline_spread']}) | {p['candidate_mean']} |",
                  f"| unwanted findings on clean cases (mean, of {q['total']} cases) | {q['baseline_mean']} "
                  f"(spread {q['baseline_spread']}) | {q['candidate_mean']} |", "",
                  f"Same {len(s['cases'])} cases on both sides, {s['n_runs']} runs each: compare case by case; a one-case change can be "
                  "run-to-run noise, and this is not a statistical test."]
        if p["total"] < MIN_POSITIVES:
            lines.append(f"Reference only: fewer than {MIN_POSITIVES} positive cases ({p['total']} now), so one case can move the totals; "
                         "read the changed cases.")
        vis, fold_why = _changed_section(s, detail)
        lines += [""] + vis
        if s.get("wrong_place"):
            lines.append("`wrong place` = a finding on the right file at another line; it is not scored: a human decides whether the "
                         "expected range needs widening.")
    if fold_why:
        lines += [""] + fold_why
    if s["cases"] and key not in ("no_baseline", "not_run"):
        n = s["n_runs"]
        rows = _case_rows(s, detail, bool(s.get("wrong_place")))
        counts = {k: sum(1 for r in rows if r[2] and r[2][1].endswith(k)) for k in ("newly missed", "newly caught", "newly noisy")}
        unchanged = sum(1 for r in rows if not r[2])
        lines += ["", f"<details><summary>Per case ({len(rows)}): newly missed {counts['newly missed']}, newly caught {counts['newly caught']}, "
                      f"newly noisy {counts['newly noisy']}, {unchanged} unchanged</summary>", ""]
        lines += _table(rows, n, bool(s.get("wrong_place")))
        lines += ["", "</details>"]
    if groups:
        lines += ["", f"<details><summary>Reviewer files changed ({n_files}): {gtext}</summary>", ""]
        lines += [f"- {g} ({len(v)}): {_file_list(v)}" for g, v in groups]
        lines += ["", "</details>"]
    lines += ["", "<details><summary>Machine-read result (CI writes it; do not edit)</summary>", "",
              "```yaml agentive-reviewer-eval", ayaml.dumps(s).rstrip("\n"), "```"]
    tail = ([f"adopt_suggested: false — merge only with `eval-override: <reason>` in the PR body ({OVERRIDE_FACT})."]
            if not s["adopt_suggested"] else []) + [f"- Note: {n}" for n in notes]
    lines += ([""] + tail if tail else []) + ["", "</details>"]
    return "\n".join(lines) + "\n"


def summary_line(repo, base, head, case_dir="evals/reviewer", cause=None):
    """E2E-25: the line of the agentive-review-summary comment for a reviewer change that the reviewer-eval comment cannot
    score because the case set is too small (reason insufficient_cases there), or None. Deterministic, no model call: the
    reviewer-change paths, the baseline rule of E2E-16 (no baseline -> None, that comment needs no eval-override) and the case
    files of the checkout [design judgment: only this reason is mirrored; the score itself exists only in the reviewer-eval job].
    E2E-42: cause (`fork` | `no_secret`, from llmstate.credential_cause) is why the reviewer-eval job cannot run the reviewer at all;
    that comment says "not run" before it looks at the cases, so this line says the same, in the same words."""
    changed, _ = reviewer_change(repo, base, head)
    if not changed or not has_baseline(repo, base):
        return None
    if cause in NOT_RUN_CAUSES:
        why = "fork PR: GitHub gives it no repository secrets" if cause == "fork" else "no AI credential secret in this repository"
        return f"not run ({why}); merging needs `eval-override: <reason>` in the PR body; details in the reviewer-eval comment."
    cases, _ = load_cases(os.path.join(repo, case_dir))
    labels = {c["label"] for c in cases}
    if {"positive", "negative"} <= labels:
        return None
    why = "evals/reviewer/ has no case yet" if not cases else "evals/reviewer/ needs a positive and a negative case"
    # E2E-28 (decision e2e_28_31) and COMMENTUX: one short clause after **Reviewer eval:**; the reviewer-eval comment keeps the
    # details (the example reason, what the line is for, how to have later reviewer changes scored)
    return f"not scored ({why}); merging needs `eval-override: <reason>` in the PR body; details in the reviewer-eval comment."
