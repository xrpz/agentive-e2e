# agentive-unit: lib.agentive
# design: spec.review_pipeline.reviewer_eval.intake, spec.review_pipeline.reviewer_eval.scoring, spec.maintenance.distill, contracts[reviewer-eval-case], decisions.accepted[EVALBUNDLE] (owner C)
"""Draft a reviewer eval case for the repo skill /distill (python3 stdlib only; no network, no model call).

  python3 -m agentive.evalcase draft positive --id ID --pr REF --base-sha SHA --head-sha SHA --origin revert|fix_pr
      --origin-ref REF  ( --fix-base SHA --fix-head SHA | --range FILE:FROM-TO ... )  [--bug-class C] [--note T] [--slack N]
  python3 -m agentive.evalcase draft negative --id ID --pr REF --base-sha SHA --head-sha SHA --forbid FILE:FROM-TO ...
      --reply wrong|intentional [--finding-id F] [--bug-class C] [--note T]

A positive case needs a fix location: the narrowest hunk of the fix (--fix-base/--fix-head, the old-side lines mapped onto the
original head and cut to the original PR's added lines) or a location given with --range (a human or the agent read it from the
fix). A revert alone changes the whole PR, so it names no location: no case is written and the output says so (EVALBUNDLE).
A negative case's forbidden ranges are the file:line of the findings answered wrong or intentional, on the head of the round on
which they were posted. Both are written to evals/reviewer/<id>.yaml with the optional fields (base_sha, head_sha, bug_class, note,
source, created, drafted_by) after a dedup check against the existing cases, and are checked with the same validate_case the
reviewer eval uses. The human merge of the /distill PR is the location check (range_basis stays `proposed`).

Output (agent-facing, the last line ends with NEXT=):
  EVALCASE=written id=.. path=.. label=.. range=..; NEXT=..      (+ EVALCASE_BODY=<one PR body line>, EVALCASE_WARN=<reason> ...)
  EVALCASE=skipped id=.. reason=no_fix_location|reply_not_forbidden; NEXT=..
  EVALCASE=duplicate id=.. existing=<id> reason=..; NEXT=..
  EVALCASE=stop reason=..; NEXT=..                               (exit 2: a wrong argument or a location outside the file)
"""
import argparse
import json
import os
import re
import sys
import time

from agentive import ayaml
from agentive.review import evals, gitio

WIDE_LINES = 20          # [design judgment] a range wider than this weakens the location score; the PR body warns
RANGE = re.compile(r"^(?P<file>.+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SAFE_PLAIN = re.compile(r"^[A-Za-z0-9_./@+-]+$")


class Stop(Exception):
    pass


# ---------------------------------------------------------------- git: hunks and line mapping
def hunks(repo, a, b, path):
    """[(old_start, old_len, new_start, new_len)] of `git diff -U0 a b -- path` (a missing length is 1)."""
    out = []
    for ln in gitio.git(repo, "diff", "-U0", "--no-color", "--no-renames", a, b, "--", path, check=False).splitlines():
        m = HUNK.match(ln)
        if m:
            out.append((int(m.group(1)), 1 if m.group(2) is None else int(m.group(2)),
                        int(m.group(3)), 1 if m.group(4) is None else int(m.group(4))))
    return out


def added_ranges(repo, base, head, path):
    """[(from, to)] lines the original PR added to path (new-side numbers at head)."""
    return [(c, c + d - 1) for _, _, c, d in hunks(repo, base, head, path) if d > 0]


def map_line(hs, line):
    """The line of the NEW side of the diff `old new` that old-side `line` is, or None when a hunk changed that line."""
    off = 0
    for a, b, _, d in hs:
        if b == 0:                 # pure insertion after old line a
            if line <= a:
                break
            off += d
            continue
        if line < a:
            break
        if line <= a + b - 1:
            return None
        off += d - b
    return line + off


def fix_candidates(repo, base, head, fix_base, fix_head):
    """(candidates, notes): candidates = [(width, file, from, to)] of the fix hunks that overlap the original PR's added lines,
    each cut to the added lines it overlaps, in head coordinates. The fix diff runs from the merge base of the fix (the fix
    base may have moved on). A hunk whose lines the original head changed again cannot be mapped and is named in notes."""
    mb = gitio.merge_base(repo, fix_base, fix_head) or fix_base
    files = [f for f in gitio.git(repo, "diff", "--name-only", "--no-renames", base, head).splitlines() if f]
    cands, notes = [], []
    for f in sorted(files):
        added = added_ranges(repo, base, head, f)
        if not added:
            continue
        same = gitio.show(repo, mb, f) == gitio.show(repo, head, f)
        to_head = [] if same else hunks(repo, mb, head, f)
        for a, b, _, _ in hunks(repo, mb, fix_head, f):
            lo, hi = (a, a + b - 1) if b > 0 else (max(a, 1), a + 1)        # a pure insertion: the two lines it sits between
            mlo, mhi = (lo, hi) if same else (map_line(to_head, lo), map_line(to_head, hi))
            if mlo is None or mhi is None or mhi - mlo != hi - lo:
                notes.append(f"fix hunk at {f}:{lo}-{hi} cannot be mapped onto the original head")
                continue
            for x, y in added:
                s, e = max(mlo, x), min(mhi, y)
                if s <= e:
                    cands.append((e - s + 1, f, s, e))
    return sorted(set(cands)), notes


# ---------------------------------------------------------------- the case file
def parse_range(text):
    m = RANGE.match(text or "")
    if not m:
        raise Stop(f"bad_range {text!r}; NEXT=write the location as FILE:FROM-TO (or FILE:LINE)")
    a, b = int(m.group("a")), int(m.group("b") or m.group("a"))
    if not 1 <= a <= b:
        raise Stop(f"bad_range {text!r}; NEXT=write the location as FILE:FROM-TO with 1 <= FROM <= TO")
    return {"file": m.group("file"), "line_from": a, "line_to": b}


def _scalar(v):
    return v if isinstance(v, str) and SAFE_PLAIN.match(v) else json.dumps(v, ensure_ascii=False)


def _rng(r):
    s = f"{{file: {_scalar(r['file'])}, line_from: {r['line_from']}, line_to: {r['line_to']}"
    return s + (f", slack: {r['slack']}}}" if "slack" in r else "}")


def marker(pr, head_sha):
    """The dedupe marker `eval-<original PR>` (design intake.dedupe); a base..head case uses the head's short sha."""
    pr = str(pr).strip()
    return "eval-" + (pr.lstrip("#") if re.match(r"^#?\d+$", pr) else head_sha[:7])


def render(case, source_note):
    c = case
    lines = [f"# agentive-check: {marker(c['pr'], c['head_sha'])} ({source_note})",
             f"id: {c['id']}", f"pr: {json.dumps(str(c['pr']))}", f"base_sha: {c['base_sha']}", f"head_sha: {c['head_sha']}",
             f"label: {c['label']}"]
    if c["label"] == "positive":
        lines += [f"origin: {{kind: {c['origin']['kind']}, ref: {json.dumps(c['origin']['ref'])}}}", "expected_findings:"]
        lines += [f"  - {_rng(r)}" for r in c["expected_findings"]]
    else:
        lines += ["forbidden_findings:"] + [f"  - {_rng(r)}" for r in c["forbidden_findings"]]
    for k in ("bug_class", "note"):
        if c.get(k):
            lines.append(f"{k}: {json.dumps(c[k], ensure_ascii=False) if k == 'note' else c[k]}")
    src = c.get("source") or {}
    lines.append("source: {" + ", ".join(f"{k}: {_scalar(v)}" for k, v in src.items()) + "}")
    lines += [f"created: {c['created']}", "drafted_by: distill"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- dedup
def _overlap(r, s):
    return r["file"] == s["file"] and r["line_from"] <= s["line_to"] and s["line_from"] <= r["line_to"]


def find_duplicate(case_dir, new, mark):
    """(existing id, reason) of a case that already covers the new one, else None: the same id, the dedupe marker in a case of the
    same label, a negative with the same source finding, or the same pr / head_sha with an overlapping range of the same label."""
    key = "expected_findings" if new["label"] == "positive" else "forbidden_findings"
    if os.path.exists(os.path.join(case_dir, new["id"] + ".yaml")):
        return new["id"], "a case file with this id exists"
    for name in sorted(os.listdir(case_dir)) if os.path.isdir(case_dir) else []:
        if not name.endswith(".yaml"):
            continue
        path = os.path.join(case_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
            doc = ayaml.loads(raw)
        except (OSError, ayaml.YAMLError):
            continue
        if not isinstance(doc, dict) or doc.get("label") != new["label"]:
            continue
        cid = str(doc.get("id", name[:-5]))
        if f"agentive-check: {mark}" in raw:
            return cid, f"it carries the marker agentive-check: {mark}"
        fid = (new.get("source") or {}).get("finding_id")
        if fid and (doc.get("source") or {}).get("finding_id") == fid:
            return cid, f"it is for the same finding {fid}"
        same_commit = str(doc.get("pr")) == str(new["pr"]) or (doc.get("head_sha") and doc.get("head_sha") == new["head_sha"])
        if same_commit and any(_overlap(r, s) for r in doc.get(key) or [] for s in new[key] if isinstance(r, dict)):
            return cid, "same PR and an overlapping range"
    return None


# ---------------------------------------------------------------- draft
def _check_sha(repo, name, sha):
    if not (isinstance(sha, str) and evals.SHA40.match(sha)):
        raise Stop(f"bad_{name} {sha!r}; NEXT=pass the full 40-character sha (gh api repos/{{owner}}/{{repo}}/pulls/<n> --jq '.base.sha, .head.sha')")
    if not gitio.ok(repo, "cat-file", "-e", sha + "^{commit}"):
        raise Stop(f"missing_{name} {sha[:12]}; NEXT=git fetch origin pull/<n>/head (the commit is not in this clone), then run it again")


def _check_ranges(repo, head, rngs):
    """Every range lies inside its file at head; returns the warnings (wide range, range outside the added lines)."""
    warns = []
    for r in rngs:
        text = gitio.show(repo, head, r["file"])
        if text is None:
            raise Stop(f"range_file_missing {r['file']}; NEXT=the file is not at head_sha {head[:12]}: check the path or the sha")
        if r["line_to"] > len(text.splitlines()):
            raise Stop(f"range_outside_file {r['file']}:{r['line_from']}-{r['line_to']}; NEXT=the file has {len(text.splitlines())} lines at head_sha "
                       f"{head[:12]}: the line numbers must be those of head_sha")
        if r["line_to"] - r["line_from"] + 1 > WIDE_LINES:
            warns.append(f"range_wide {r['file']}:{r['line_from']}-{r['line_to']} ({r['line_to'] - r['line_from'] + 1} lines; a wide range makes the location score weak)")
    return warns


def draft(repo, a, today=None):
    """Run one draft; returns (status, lines). status: written | skipped | duplicate. Raises Stop for a wrong argument."""
    case_dir = os.path.join(repo, a.dir)
    if not evals.CASE_ID.match(a.id or ""):
        raise Stop(f"bad_id {a.id!r}; NEXT=use lowercase letters, digits, . _ - (it is the file name)")
    if not str(a.pr or "").strip():
        raise Stop("pr_missing; NEXT=pass --pr #N (or <base sha>..<head sha>)")
    _check_sha(repo, "base_sha", a.base_sha)
    _check_sha(repo, "head_sha", a.head_sha)
    if a.bug_class and a.bug_class not in evals.BUG_CLASSES:
        raise Stop(f"bad_bug_class {a.bug_class!r}; NEXT=use one of {', '.join(evals.BUG_CLASSES)}")
    case = {"id": a.id, "pr": str(a.pr).strip(), "base_sha": a.base_sha, "head_sha": a.head_sha, "label": a.label,
            "created": today or time.strftime("%Y-%m-%d", time.gmtime())}
    extra, warns, how = [], [], ""
    if a.label == "positive":
        if a.origin not in ("revert", "fix_pr") or not a.origin_ref:
            raise Stop("origin_missing; NEXT=pass --origin revert|fix_pr and --origin-ref <PR or commit>")
        case["origin"] = {"kind": a.origin, "ref": a.origin_ref}
        if a.range:
            rngs = [parse_range(x) for x in a.range]
            how = "location given with --range"
        elif a.fix_base and a.fix_head:
            _check_sha(repo, "fix_base", a.fix_base)
            _check_sha(repo, "fix_head", a.fix_head)
            cands, notes = fix_candidates(repo, a.base_sha, a.head_sha, a.fix_base, a.fix_head)
            warns += [f"unmapped {n}" for n in notes]
            if not cands:
                return "skipped", [f"EVALCASE=skipped id={a.id} reason=no_fix_location; NEXT=the fix does not touch lines this PR added "
                                   "(or its hunks cannot be mapped): write 'eval case not written: no fix location' under missing_context in the PR "
                                   "body, or ask the human for FILE:FROM-TO and run it again with --range"]
            rngs = [{"file": cands[0][1], "line_from": cands[0][2], "line_to": cands[0][3]}]
            if len(cands) > 1:
                extra = [f"{f}:{s}-{e}" for _, f, s, e in cands[1:6]]
            how = f"narrowest fix hunk of {a.fix_head[:7]}"
        else:
            return "skipped", [f"EVALCASE=skipped id={a.id} reason=no_fix_location; NEXT=a revert changes the whole PR, so it names no location: "
                               "write 'eval case not written: revert only, no fix location' under missing_context in the PR body "
                               "(with a human present, ask for FILE:FROM-TO and run it again with --range)"]
        if a.slack is not None:
            if not 0 <= a.slack <= evals.MAX_SLACK:
                raise Stop(f"bad_slack {a.slack}; NEXT=use 0..{evals.MAX_SLACK}")
            rngs[0]["slack"] = a.slack
        case["expected_findings"] = rngs
        case["source"] = {"range_basis": "proposed"}
        key, source_note = "expected_findings", f"from {'PR ' + case['pr'] if case['pr'].startswith('#') else case['pr']}; drafted by /distill, {how}, location proposed"
    else:
        if a.reply not in ("wrong", "intentional", "not_important"):
            raise Stop("reply_missing; NEXT=pass --reply wrong|intentional|not_important (the reason of the agentive-reply line)")
        if a.reply == "not_important":
            return "skipped", [f"EVALCASE=skipped id={a.id} reason=reply_not_forbidden; NEXT=a not_important answer raises the bar in code_review.md "
                               "instead; a negative case needs a finding answered wrong or intentional"]
        if not a.forbid:
            raise Stop("forbid_missing; NEXT=pass --forbid FILE:FROM-TO for each finding answered wrong or intentional (its line on head_sha)")
        rngs = [parse_range(x) for x in a.forbid]
        case["forbidden_findings"] = rngs
        case["source"] = {k: v for k, v in (("finding_id", a.finding_id), ("reply", a.reply), ("range_basis", "proposed")) if v}
        key, source_note = "forbidden_findings", f"from {'PR ' + case['pr'] if case['pr'].startswith('#') else case['pr']}; drafted by /distill, finding location"
    if a.bug_class:
        case["bug_class"] = a.bug_class
    note = (a.note or "").strip()
    if extra:
        note = (note + "; " if note else "") + "other fix hunks: " + ", ".join(extra)
    if note:
        case["note"] = note[:300]
    warns += _check_ranges(repo, a.head_sha, case[key])
    if a.label == "positive":
        for r in case[key]:
            if not any(_overlap(r, {"file": r["file"], "line_from": x, "line_to": y}) for x, y in added_ranges(repo, a.base_sha, a.head_sha, r["file"])):
                warns.append(f"range_not_in_added_lines {r['file']}:{r['line_from']}-{r['line_to']} (the PR did not add these lines)")
    dup = find_duplicate(case_dir, case, marker(case["pr"], a.head_sha))
    if dup:
        return "duplicate", [f"EVALCASE=duplicate id={a.id} existing={dup[0]} reason={dup[1]}; NEXT=do not add a second case: improve "
                             f"evals/reviewer/{dup[0]}.yaml if its range is wrong, and say so in the PR body"]
    text = render(case, source_note)
    probs = evals.validate_case(ayaml.loads(text), a.id)
    if probs:
        raise Stop("invalid_case " + "; ".join(probs) + "; NEXT=fix the arguments and run it again")
    os.makedirs(case_dir, exist_ok=True)
    path = os.path.join(case_dir, a.id + ".yaml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    rel = os.path.join(a.dir, a.id + ".yaml")
    spot = ", ".join(f"{r['file']}:{r['line_from']}-{r['line_to']}" for r in case[key])
    out = [f"EVALCASE_BODY=- `{rel}` ({a.label}, from {case['pr']}): `{spot}`, location proposed ({how or 'finding location'}); "
           "the human merge confirms the location"]
    out += [f"EVALCASE_WARN={w}" for w in warns]
    out.append(f"EVALCASE=written id={a.id} path={rel} label={a.label} range={spot}; NEXT=commit it with the fix in the /distill PR and "
               "list it in the PR body with the EVALCASE_BODY line")
    return "written", out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentive.evalcase")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("label", choices=["positive", "negative"])
    d.add_argument("--id", required=True)
    d.add_argument("--pr", required=True)
    d.add_argument("--base-sha", required=True)
    d.add_argument("--head-sha", required=True)
    d.add_argument("--origin")
    d.add_argument("--origin-ref")
    d.add_argument("--fix-base")
    d.add_argument("--fix-head")
    d.add_argument("--range", action="append")
    d.add_argument("--slack", type=int)
    d.add_argument("--forbid", action="append")
    d.add_argument("--finding-id")
    d.add_argument("--reply")
    d.add_argument("--bug-class")
    d.add_argument("--note")
    d.add_argument("--dir", default="evals/reviewer")
    d.add_argument("--created")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    repo = gitio.git(os.getcwd(), "rev-parse", "--show-toplevel", check=False).strip() or os.getcwd()
    try:
        _, lines = draft(repo, a, today=a.created)
    except Stop as e:
        print(f"EVALCASE=stop reason={e}")
        return 2
    except gitio.GitError as e:
        print(f"EVALCASE=stop reason=git_failed ({e}); NEXT=fetch the commits (git fetch origin pull/<n>/head) and run it again")
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
