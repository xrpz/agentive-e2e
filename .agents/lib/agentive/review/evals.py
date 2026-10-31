# agentive-unit: lib.agentive
# design: spec.review_pipeline.reviewer_eval, spec.review_pipeline.reviewer_eval.scoring, spec.review_pipeline.reviewer_eval.ci, spec.review_pipeline.reviewer_eval.reviewer_paths, contracts[reviewer-eval-case] (owner D)
"""Reviewer eval: which PRs are a reviewer change, case files, scoring and the summary block. No model calls here:
the runner (.agents/bin/agentive-reviewer-eval run) starts the headless runs; this module is deterministic.

Scoring (design): counted = findings the pipeline would post (after RV7 and RV6) with severity important, on a line;
a counted finding catches a range when its file is equal and its line or one of its instances[].line is in
[line_from, line_to]. Positive pass = every expected range caught; negative pass = no blocking finding and no counted
finding in a forbidden range. Each side runs N times; a case's result is its majority; side counts are means.
A failed run (no report) counts as a failed run of that case [design judgment].
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
MARKER = "<!-- agentive-reviewer-eval -->"


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


# ---------------------------------------------------------------- cases
def validate_case(doc, file_id):
    p = []
    if not isinstance(doc, dict):
        return ["case must be a mapping"]
    extra = set(doc) - {"id", "pr", "label", "expected_findings", "budget_usd", "origin", "forbidden_findings"}
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
        for r in v:
            if not (isinstance(r, dict) and isinstance(r.get("file"), str) and r["file"] and isinstance(r.get("line_from"), int)
                    and isinstance(r.get("line_to"), int) and 1 <= r["line_from"] <= r["line_to"] and set(r) <= {"file", "line_from", "line_to"}):
                q.append(f"{key} entry {r!r} needs file, line_from <= line_to")
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
        out.append({"file": f["file"], "line": f["line"], "instances": f.get("instances") or [], "blocking": bool(f.get("blocking"))})
    return out


def catches(finding, rng):
    spots = [(finding["file"], finding["line"])] + [(i.get("file", finding["file"]), i["line"]) for i in finding.get("instances") or []]
    return any(f == rng["file"] and rng["line_from"] <= ln <= rng["line_to"] for f, ln in spots)


def run_result(case, run):
    """(passed, unwanted) of one run: run = counted findings, or None for a failed run."""
    if run is None:
        return False, 1 if case["label"] == "negative" else 0
    if case["label"] == "positive":
        ok = all(any(catches(f, r) for f in run) for r in case["expected_findings"])
        return ok, 0
    bad = [f for f in run if f["blocking"] or any(catches(f, r) for r in case.get("forbidden_findings") or [])]
    return not bad, len(bad)


def _side(cases, runs_by_case, n):
    per_run_caught, per_run_unwanted, results = [0] * n, [0] * n, {}
    for c in cases:
        runs = list(runs_by_case.get(c["id"]) or [])
        runs += [None] * (n - len(runs))
        passes = 0
        for i, r in enumerate(runs[:n]):
            ok, unwanted = run_result(c, r)
            passes += 1 if ok else 0
            if c["label"] == "positive" and ok:
                per_run_caught[i] += 1
            if c["label"] == "negative":
                per_run_unwanted[i] += unwanted
        results[c["id"]] = passes
    return per_run_caught, per_run_unwanted, results


def summarize(cases, baseline_runs, candidate_runs, n, baseline_ref, candidate_ref, not_run=False):
    """reviewer-eval-case.schema.json $defs.summary."""
    pos = [c for c in cases if c["label"] == "positive"]
    neg = [c for c in cases if c["label"] == "negative"]
    b_caught, b_unw, b_res = _side(cases, baseline_runs, n)
    c_caught, c_unw, c_res = _side(cases, candidate_runs, n)
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
    if not_run:
        summary.update(adopt_suggested=False, reason="not_run")
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


def comment(summary, reasons, notes=()):
    s = summary
    lines = [MARKER, "### agentive reviewer eval (before / after)", "",
             f"Reviewer change: {'; '.join(reasons) or '-'}", "",
             f"| | before ({s['baseline_ref'][:12]}) | after ({s['candidate_ref'][:12]}) |", "|---|---|---|",
             f"| positives caught (mean of {s['n_runs']} runs, of {s['positives']['total']}) | {s['positives']['baseline_mean']} "
             f"(spread {s['positives']['baseline_spread']}) | {s['positives']['candidate_mean']} |",
             f"| unwanted findings on negatives (mean, of {s['negatives']['total']} cases) | {s['negatives']['baseline_mean']} "
             f"(spread {s['negatives']['baseline_spread']}) | {s['negatives']['candidate_mean']} |", ""]
    if s["cases"]:
        lines += ["| case | label | before pass runs | after pass runs |", "|---|---|---|---|"]
        lines += [f"| {c['id']} | {c['label']} | {c['baseline_pass_runs']}/{s['n_runs']} | {c['candidate_pass_runs']}/{s['n_runs']} |" for c in s["cases"]]
        lines.append("")
    if s["reason"] == "insufficient_cases":
        lines.append("reviewer eval cases insufficient — change not verified")
    if s["reason"] == "not_run":
        lines.append("not run: this run has no secrets (fork PR of a public repository)")
    for n in notes:
        lines.append(f"- note: {n}")
    lines += ["", "```yaml agentive-reviewer-eval", ayaml.dumps(s).rstrip("\n"), "```", ""]
    if not s["adopt_suggested"]:
        lines.append("adopt_suggested: false — merge only with 'eval-override: <reason>' in the PR body")
    return "\n".join(lines) + "\n"
