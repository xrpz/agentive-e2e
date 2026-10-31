# agentive-unit: lib.agentive
# design: spec.work_loop.pr_skill, spec.work_loop.pr_explainer, spec.verification.E1a, spec.verification.E1b, contracts[pr-body-fw-review], spec.spec_stage.chain.ci_check.link, spec.spec_stage.triage.user_override.skip_hard_to_reverse, decisions.accepted[WFCHANGE], decisions.accepted[BASEREF], decisions.accepted[VERIFYTREE]
"""PR body for the repo skill /pr (owner C; python3 stdlib only). Called by .agents/skills/pr/scripts/pr-body.sh.

  draft   [--explain] [--tool T] [--model M] [--session-id S] [--intent-skipped] [--hotfix-of N] [--out F]
          write the body skeleton: deterministic parts filled (## Verification from .agents/state/verify.json,
          ## Risk from plan.md ## Risks, the agentive-review block), `<!-- agent: ... -->` markers where the agent
          writes prose; ## Explainer first when the local rules predict tier 2 or leave the tier uncertain
  check   <file> [--explainer required|optional]   headings, Explainer presence, markers, agentive-review block
  publish <file> [--title T]   open the PR (gh api POST) or, on an open PR, replace only ## Verification
          (an --explain draft: only ## Explainer) with gh api PATCH
Every run ends with one line for the agent; problems end with `NEXT=`. draft fetches origin/<default> once with an
explicit refspec first (gitgh.base_ref, BASEREF): a missing base is a stop (base_ref_missing, no_merge_base,
no_commits_ahead), never "no diff".
Tier prediction is a local estimate from the agentive-review-rules block; CI (RV4, RV8) is authoritative.
"""
import argparse
import datetime
import json
import os
import re
import sys

from agentive import ayaml
from agentive import verb as verbmod
from agentive.review import config as reviewcfg
from agentive.review import gitio as reviewgit
from agentive.review import rv3 as reviewrv3
from agentive.review import secpaths
from agentive.gitgh import GhError, base_ref, gh, gh_json, git, no_human, origin_repo

VERBS = ["setup", "build", "lint", "typecheck", "test", "verify", "run"]
ALL_TIERS = ["## Verification", "## Risk", "## How to revert"]
SLUG = re.compile(r"^(?!archive$)[a-z0-9]+(?:-[a-z0-9]+)*$")
CHAIN_PATH = re.compile(r"^docs/plans/([^/]+)/(intent|spec|plan)\.md$")
MARKER = re.compile(r"<!-- agent:|%% agent:")
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
             "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"}
REGIONS = {"AGENTS.md": ["context", "path-rules"], "CLAUDE.md": ["imports"]}
INTENT_SKIPPED_LINE = "intent skipped: user instruction"
# parsers accept the Korean line that /pr in agentive v0.1.0 wrote, so PRs opened by that version still read as intent-skipped
# [design judgment]. The Korean words are written as \u escapes so that no Hangul stays in the installed source.
INTENT_SKIPPED_RE = re.compile(
    r"(?m)^[ \t]*(?:intent skipped: user instruction"
    r"|intent \uc0dd\ub7b5: \uc0ac\uc6a9\uc790 \uc9c0\uc2dc)[ \t]*$")
# E2E-11 (decisions.accepted[WFCHANGE]): the PR that adds or changes the review workflow gets no AI review on the claude CI path
WORKFLOW_CHANGE_LINE = ("AI review: this PR adds or changes .github/workflows/agentive-review.yml, so the Claude Code action skips "
                        "the CI AI review (its workflow validation needs the file identical to the default branch's copy); a human "
                        "reviews and merges this PR (the gate ends human_required), and later PRs get the AI review once it is "
                        "merged. Merge the install PR first, before other PRs.")
LEGEND = "Legend: green = added · amber = changed · red dashed = removed · solid arrow = runs · dashed arrow = removed flow"


# ---------------------------------------------------------------- helpers
def glob_re(pattern):
    """Repo-root-relative glob: ** spans directories, * and ? stay inside one path segment."""
    i, out = 0, ""
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile("^" + out + "$")


def glob_match(pattern, path):
    return bool(glob_re(pattern).match(path))


def fences(lines):
    """[(line, inside_code_fence)] where fence lines themselves count as inside."""
    res, inside = [], False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            res.append((ln, True))
            inside = not inside
            continue
        res.append((ln, inside))
    return res


def rules_block(text):
    """The agentive-review-rules mapping from code_review.md text, or None."""
    m = re.search(r"^```yaml agentive-review-rules[ \t]*\n(.*?)^```[ \t]*$", text or "", re.S | re.M)
    if not m:
        return None
    try:
        doc = ayaml.loads(m.group(1))
    except ayaml.YAMLError:
        return None
    rules = (doc or {}).get("agentive-review-rules")
    if isinstance(rules, dict) and isinstance(rules.get("trust_boundary"), dict) and not reviewcfg.validate_rules({"agentive-review-rules": rules}):
        rules = dict(rules, trust_boundary=reviewcfg.effective_trust_boundary(rules["trust_boundary"]))   # SECPATHS: floor + block + add - remove
    return rules


def region_body(text, region):
    m = re.search(r"<!-- AGENTIVE START %s -->\n(.*?)<!-- AGENTIVE END %s -->" % (re.escape(region), re.escape(region)),
                  text or "", re.S)
    return m.group(1) if m else None


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def section(text, heading):
    """Body of a level-2 section of a markdown file (until the next level-2 heading), or None."""
    lines, out, on = text.splitlines(), [], False
    for ln, inside in fences(lines):
        if not inside and ln.startswith("## "):
            if on:
                break
            on = ln.strip() == heading
            continue
        if on:
            out.append(ln)
    return "\n".join(out).strip("\n") if on or out else None


# ---------------------------------------------------------------- repository state
class Repo:
    """The work branch against origin/<default> (BASEREF, E2E-06). `notes` are NOTE lines for stdout; `stop` is the
    stop line when there is no base to compare with: base_ref_missing / no_default_branch (gitgh.base_ref),
    no_merge_base (with the shallow flag) or no_commits_ahead. Commits whose net diff is empty leave `changes` empty
    and `ahead` > 0 (tier uncertain, reason empty diff). fetch=False (publish) reads the local refs only."""

    def __init__(self, root, fetch=True):
        self.root = root
        self.branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        self.head = git(root, "rev-parse", "HEAD")
        b = base_ref(root, fetch=fetch)
        self.default, self.base_ref, self.notes, self.stop = b["name"], b["ref"], list(b["notes"]), b["stop"]
        self.base, self.ahead = None, 0
        if not self.stop and self.branch not in (self.default, "HEAD"):
            self.base = git(root, "merge-base", self.base_ref, "HEAD")
            if not self.base:
                shallow = git(root, "rev-parse", "--is-shallow-repository") == "true"
                nxt = ("git fetch --unshallow origin (or git fetch --deepen=200 origin)" if shallow else
                       f"rebase the work branch onto origin/{self.default} (the histories are unrelated)")
                self.stop = (f"AGENTIVE_PR=stop reason=no_merge_base base=origin/{self.default} shallow={str(shallow).lower()}; "
                             f"NEXT={nxt}, then rerun sh .agents/skills/pr/scripts/pr-body.sh draft")
            else:
                self.ahead = int(git(root, "rev-list", "--count", f"{self.base_ref}..HEAD") or 0)
                if self.ahead == 0:
                    self.stop = (f"AGENTIVE_PR=stop reason=no_commits_ahead base=origin/{self.default}; NEXT=commit the change on "
                                 "this branch (or switch to the branch that holds it), then rerun sh .agents/skills/pr/scripts/pr-body.sh draft")
        self.changes = self._changes()

    def _changes(self):
        """[(status, path, added, deleted)] of base...HEAD (renames keep the new path)."""
        if not self.base:
            return []
        ns = git(self.root, "diff", "--name-status", "-M", f"{self.base}...HEAD") or ""
        st = {}
        for ln in ns.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                st[parts[-1]] = parts[0][0]
        res = []
        num = git(self.root, "diff", "--numstat", "-M", f"{self.base}...HEAD") or ""
        for ln in num.splitlines():
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            path = parts[2]
            if " => " in path:  # rename shown as a/{x => y}/b or x => y
                path = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", path)
                path = path.split(" => ")[-1].replace("//", "/")
            a = int(parts[0]) if parts[0].isdigit() else 0
            d = int(parts[1]) if parts[1].isdigit() else 0
            res.append((st.get(path, "M"), path, a, d))
        return res

    def show(self, rev, path):
        return git(self.root, "show", f"{rev}:{path}")

    def rules(self):
        """The review rules from code_review.md on origin/<default>; when that commit has none (the install PR), from
        the work tree, with a NOTE: then the PR branch's own rules decide the local estimate (CI reads the base)."""
        src = self.show(f"refs/remotes/origin/{self.default}", "code_review.md") if self.default else None
        got = rules_block(src)
        if got:
            return got
        local = rules_block(read(os.path.join(self.root, "code_review.md")))
        if local:
            self.notes.append(f"NOTE rules from the work tree: origin/{self.default} has no code_review.md rules block "
                              "(local estimate only; CI reads the base commit)")
        return local or {}


def chain_link(repo):
    """({slug, stage} or None, problems[])"""
    slugs, kinds, problems = set(), {}, []
    for status, path, _, _ in repo.changes:
        m = CHAIN_PATH.match(path)
        if m and m.group(1) != "archive" and status in ("A", "M"):
            slugs.add(m.group(1))
            kinds.setdefault(m.group(1), set()).add((m.group(2), status))
    bm = re.match(r"^(intent|spec|feature)/(.+)$", repo.branch or "")
    if bm and SLUG.match(bm.group(2)):
        slugs.add(bm.group(2))
    if not slugs:
        return None, problems
    if len(slugs) > 1:
        problems.append("multiple_slugs: split the PR, one slug per PR")
    slug = sorted(slugs)[0]
    names = {k for k, _ in kinds.get(slug, set())}
    stage = "feature" if ("plan" in names or (bm and bm.group(1) == "feature")) else \
        "spec" if ("spec" in names or (bm and bm.group(1) == "spec")) else "intent"
    new_artifact = any(k in ("intent", "spec") and s == "A" for k, s in kinds.get(slug, set()))
    if new_artifact and len(repo.changes) > 1:
        problems.append("mixed_artifacts: a new intent.md or spec.md must be the only changed path")
    return {"slug": slug, "stage": stage}, problems


def confirm_hits(repo, rules):
    globs = ((rules.get("trust_boundary") or {}).get("confirm_scope_globs") or {})
    hits = []
    for _, path, _, _ in repo.changes:
        for cat, gl in globs.items():
            if secpaths.first_dir_match(gl, path):
                hits.append(path)
                break
    return hits


def u2_block(repo, rules):
    hits = confirm_hits(repo, rules)
    rec = load_json(os.path.join(repo.root, ".agents", "state", "precommit-review.json")) or {}
    ru = rec.get("u2") or {}
    run = ru.get("run") or ("non_interactive" if no_human() else "interactive")
    if not hits:
        conf = "not_required"
    else:
        rec_paths = {h.get("path") for h in rec.get("confirm_scope_hits") or [] if isinstance(h, dict)}
        ok = ru.get("human_confirmation") == "obtained" and run == "interactive" and set(hits) <= rec_paths
        conf = "obtained" if ok else "not_obtained"
    return {"run": run, "human_confirmation": conf, "confirm_scope_hits": hits}


def predict_tier(repo, rules, chain, u2):
    """(0 | 1 | 2 | 'uncertain', reasons[]) from the deterministic rules on the local diff."""
    if not repo.changes:
        return "uncertain", [f"empty diff: the {repo.ahead} commit(s) ahead of origin/{repo.default} change nothing in total"]
    tb, tiers, cls = rules.get("trust_boundary") or {}, rules.get("tiers") or {}, rules.get("classify") or {}
    reasons = []
    paths = [p for _, p, _, _ in repo.changes]
    diff = git(repo.root, "diff", "-U0", f"{repo.base}...HEAD") or ""
    hunks = reviewgit.parse_patch(diff)
    for p in paths:
        for item in secpaths.expand(tb.get("never_ai_approve") or []):
            if not item.get("shadow") and secpaths.path_hit(item, p):
                reasons.append(f"trust_boundary:{item.get('reason')}:{p}")
                break
        else:
            for rule in secpaths.expand(tb.get("content_rules") or []):
                if not rule.get("shadow") and secpaths.content_hit(rule, p, hunks.get(p)):
                    reasons.append(f"trust_boundary:{rule.get('reason')}:{p}")
                    break
        protected = (p.startswith(".agents/") and not p.startswith(".agents/state/")) or p.startswith((".claude/", ".codex/", "evals/reviewer/")) \
            or p == "code_review.md" or p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS") \
            or any(glob_match(g, p) for g in tb.get("protected_check_config") or [])
        if protected:
            reasons.append(f"check_config:{p}")
        if p in REGIONS:
            for r in REGIONS[p]:
                if region_body(repo.show(repo.base, p), r) != region_body(read(os.path.join(repo.root, p)), r):
                    reasons.append(f"check_config:{p}#{r}")
    if chain and chain["stage"] in ("intent", "spec"):
        reasons.append("chain_approval")
    if u2["human_confirmation"] == "not_obtained":
        reasons.append("U2.non_interactive")
    if len(paths) > 100:
        reasons.append("more_than_100_files")
    lines = sum(a + d for _, _, a, d in repo.changes)
    if lines > int(tiers.get("large_change_lines", 1000)):
        reasons.append(f"changed_lines>{tiers.get('large_change_lines', 1000)}")
    if any(ln[:1] in "+-" and not ln.startswith(("+++", "---")) and "agentive-check:" in ln for ln in diff.splitlines()):
        reasons.append("agentive-check_tag")
    if reasons:
        return 2, reasons
    classes = set()
    mb = (git(repo.root, "merge-base", repo.base, "HEAD") or "").strip() or repo.base
    for _, p, _, _ in repo.changes:
        c = None
        for rule in cls.get("path_rules") or []:
            if glob_match(rule.get("glob", ""), p):
                c = rule.get("class")
                break
        # the RV3 rule functions of the CI pipeline (V2-01, V2-02): instruction files and package or build manifests are never
        # docs, and format is the per-language whitespace check, so the local estimate never says 0 where CI says 1
        if c is None and reviewrv3.NOT_DOCS.search(p):
            c = "config"
        if c is None and os.path.basename(p) in LOCKFILES:
            c = "lockfile"
        if c is None and reviewrv3._manifest_class(p):
            c = reviewrv3._manifest_class(p)
        if c is None and cls.get("whitespace_only_is_format", True) and \
                reviewrv3.whitespace_only_texts(p, repo.show(mb, p), repo.show("HEAD", p)):
            c = "format"
        if c is None and reviewrv3._is_doc(p) and not CHAIN_PATH.match(p):
            c = "docs"
        if c is None:
            return "uncertain", [f"class undecided by rules: {p}"]
        classes.add(c)
    if classes & set(tiers.get("tier2_classes") or ["security"]):
        return 2, ["tier2_class"]
    t0 = set(tiers.get("tier0_classes") or ["docs", "format"])
    if classes <= t0 and lines <= int(tiers.get("tier0_max_changed_lines", 200)):
        return 0, ["tier0 classes " + ",".join(sorted(classes))]
    return 1, ["classes " + ",".join(sorted(classes))]


# ---------------------------------------------------------------- body parts
def verify_state(repo):
    """(record, dirty | missing | stale | reused). E1b/E1c revised (VERIFYTREE, E2E-08): the record is reused when its
    content_tree equals HEAD^{tree} and its verb commands are the manifest's, whatever commit or dirty flag it was made
    with; a commit, an amend of the message or a rebase that keeps the tree do not make it stale."""
    v = load_json(os.path.join(repo.root, ".agents", "state", "verify.json"))
    dirty_now = bool((git(repo.root, "status", "--porcelain", "--untracked-files=no") or "").strip())
    if dirty_now:
        return v, "dirty"
    if not v:
        return v, "missing"
    tree = git(repo.root, "rev-parse", "--verify", "-q", "HEAD^{tree}")
    manifest = None
    mp = os.path.join(repo.root, ".agents", "manifest.yaml")
    if os.path.isfile(mp):
        try:
            manifest = ayaml.load(mp)
        except Exception:      # an unreadable manifest: compare the tree only
            manifest = None
    return v, ("reused" if verbmod.fresh_for(v, tree, manifest) else "stale")


def requirements(repo, chain):
    if not chain:
        return []
    text = read(os.path.join(repo.root, "docs", "plans", chain["slug"], "spec.md")) or ""
    body = section(text, "## Requirements") or ""
    return [ln.strip()[2:] for ln in body.splitlines()
            if re.match(r"^\s*- R\d+\b", ln) and "(dropped)" not in ln]


def verification_section(repo, v, chain):
    out = ["## Verification", "", f"HEAD: {repo.head}"]
    if (v or {}).get("content_tree"):
        out.append(f"Tree: {v['content_tree']} (the content verify ran on; equals HEAD^{{tree}})")
    verbs = (v or {}).get("verbs") or {}
    for name in VERBS:
        r = verbs.get(name)
        if r is None:
            continue
        cmd = f"`{r['cmd']}`" if r.get("cmd") else f"`.agents/bin/verb {name}`"
        res = "pass (exit 0)" if r.get("ok") else "fail (exit non-zero)"
        out.append(f"- {name}: {cmd} -> {res}, {r.get('failures', 0)} failures")
    for name in (v or {}).get("pending") or []:
        out.append(f"- {name}: not run (pending: no stack evidence yet)")
    reqs = requirements(repo, chain)
    if reqs:
        out += ["", f"Requirements (docs/plans/{chain['slug']}/spec.md):"]
        out += [f"- {r} -> <!-- agent: pass | fail | not run -->" for r in reqs]
    out += ["", "<!-- agent: bug fix only: the red run (fails before the fix) and the green run (passes after), "
                "command and result each; UI change: a screenshot path; otherwise delete this line -->"]
    return "\n".join(out)


def explainer_section(reasons):
    why = "; ".join(reasons) if reasons else "tier uncertain from the local rules"
    return "\n".join([
        "## Explainer", "",
        "<!-- agent: 3-6 lines in the PR's language: what changes, why, and what the human must judge; cite code for "
        "every claim as path:line, path:start-end or path#symbol (head tree; removed code from the base tree) -->",
        f"Tier 2 (predicted): {why}", "",
        "### Before", "", "```mermaid", "flowchart TD",
        "  %% agent: the current workflow on the base branch for the part this PR touches; label nodes with code refs",
        "```", "",
        "### After", "", "```mermaid", "flowchart TD",
        "  %% agent: the same flow after this PR; mark nodes :::added, :::changed or :::removed; removed calls as dashed links -.->",
        "  classDef added fill:#e3f4ea,stroke:#1d7a55,stroke-width:2px",
        "  classDef changed fill:#fdf3d6,stroke:#9a6700,stroke-width:2px",
        "  classDef removed fill:#fbe7e6,stroke:#c2362f,stroke-dasharray:6 4",
        "```", "", LEGEND])


def intent_skipped_in(body):
    """True when a PR body carries the intent-skipped line (current English form or the v0.1.0 Korean form)."""
    return bool(INTENT_SKIPPED_RE.search(body or ""))


def changes_review_workflow(repo):
    """True when base...HEAD adds or changes the review workflow and the effective CI tool is claude (E2E-11)."""
    if not any(p == ".github/workflows/agentive-review.yml" and st != "D" for st, p, _, _ in repo.changes):
        return False
    def src(rel):
        return read(os.path.join(repo.root, rel))
    return reviewcfg.ci_tool(reviewcfg.pipeline_section(src), reviewcfg.load_manifest(src).value) == "claude"


def risk_section(repo, chain, intent_skipped):
    out = ["## Risk", ""]
    if changes_review_workflow(repo):
        out += [WORKFLOW_CHANGE_LINE, ""]
    plan = read(os.path.join(repo.root, "docs", "plans", chain["slug"], "plan.md")) if chain and chain["stage"] == "feature" else None
    risks = section(plan, "## Risks") if plan else None
    if risks:
        out += [f"From docs/plans/{chain['slug']}/plan.md ## Risks:", risks.strip(), ""]
    out.append("<!-- agent: what could break and how it was checked; delete this line when the lines above already say it -->")
    if intent_skipped:
        out += ["", INTENT_SKIPPED_LINE]
    return "\n".join(out)


def revert_section():
    return "\n".join(["## How to revert", "",
                      "Revert the merge commit (`git revert -m 1 <merge sha>`; a squash merge: `git revert <sha>`) on a work branch and open it as a PR.",
                      "<!-- agent: extra steps (data migration, config, caches, feature flags), or delete this line -->"])


def review_block(repo, args, u2, chain):
    rec = load_json(os.path.join(repo.root, ".agents", "state", "precommit-review.json")) or {}
    stop = load_json(os.path.join(repo.root, ".agents", "state", "stop.json")) or {}
    tool = args.tool or ("claude_code" if os.environ.get("CLAUDECODE") else "other")
    model = args.model or "unknown"
    session = args.session_id or os.environ.get("AGENTIVE_SESSION_ID") or stop.get("session_id") or "unknown"
    first = git(repo.root, "log", "--reverse", "--format=%cI", f"{repo.base}..HEAD") if repo.base else None
    started = (first.splitlines()[0] if first else None) or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = {"implementer": {"tool": tool, "model": model, "session_id": session, "started_at": started}, "reviews": []}
    rv = rec.get("reviewer")
    if isinstance(rv, dict) and rv.get("executed_by"):
        # the local record as the review script wrote it: honest advisory fields, copied; nothing is added here
        e = {"role": "reviewer", "tool": rv.get("tool") or "other", "executed_by": rv["executed_by"],
             "attestation": rv.get("attestation") or "self_reported", "context": rv.get("context") or "unverified",
             "source": "precommit-review", "verdict": "block" if rec.get("blocking") else "pass",
             "reviewer_id": rv.get("reviewer_id", "correctness-reviewer")}
        for k in ("session_id", "agent_id", "agent_type", "reviewed_ref"):
            if rv.get(k):
                e[k] = rv[k]
        block["reviews"].append(e)
    block["u2"] = u2
    if chain:
        block["chain"] = chain
    if args.hotfix_of:
        block["hotfix_of"] = args.hotfix_of
    return "```yaml agentive-review\n" + ayaml.dumps({"agentive-review": block}).rstrip("\n") + "\n```"


def find_open_pr(repo):
    """Number of the open PR of the current branch, or None (also when gh or the origin is unavailable)."""
    slug = origin_repo(repo.root)
    if not slug or not repo.branch:
        return None
    try:
        prs = gh_json(f"repos/{slug}/pulls?head={slug.split('/')[0]}:{repo.branch}&state=open") or []
    except (GhError, ValueError):
        return None
    return prs[0].get("number") if prs else None


# ---------------------------------------------------------------- check
def explainer_problems(body):
    """Presence rules of x-agentive-body-headings.tier2_explainer (the same rules RV8 applies)."""
    lines = [(ln, inside) for ln, inside in fences(body.splitlines())]
    h2 = [i for i, (ln, inside) in enumerate(lines) if not inside and ln.startswith("## ")]
    exp = [i for i in h2 if lines[i][0].strip() == "## Explainer"]
    if not exp:
        return ["no_section"]
    if h2[0] != exp[0]:
        return ["not_first"]
    end = next((i for i in h2 if i > exp[0]), len(lines))
    part = lines[exp[0] + 1:end]
    b = [i for i, (ln, inside) in enumerate(part) if not inside and ln.strip() == "### Before"]
    a = [i for i, (ln, inside) in enumerate(part) if not inside and ln.strip() == "### After"]
    probs = []
    if not b:
        probs.append("no_before")
    if not a:
        probs.append("no_after")
    if b and a and b[0] > a[0]:
        probs.append("before_order")
    if b and a and b[0] < a[0]:
        if not any(ln.strip().startswith("```mermaid") for ln, _ in part[b[0]:a[0]]):
            probs.append("before_no_mermaid")
        if not any(ln.strip().startswith("```mermaid") for ln, _ in part[a[0]:]):
            probs.append("after_no_mermaid")
    return probs


def block_problems(body):
    blocks = re.findall(r"^```yaml agentive-review[ \t]*\n(.*?)^```[ \t]*$", body, re.S | re.M)
    if len(blocks) != 1:
        return [f"expected one ```yaml agentive-review block, found {len(blocks)}"]
    try:
        doc = (ayaml.loads(blocks[0]) or {}).get("agentive-review")
    except ayaml.YAMLError as e:
        return [f"agentive-review block does not parse ({e})"]
    if not isinstance(doc, dict):
        return ["agentive-review block has no top key agentive-review"]
    probs = []
    imp = doc.get("implementer") or {}
    for k in ("tool", "model", "session_id", "started_at"):
        if not isinstance(imp.get(k), str) or not imp.get(k):
            probs.append(f"implementer.{k} missing")
    if imp.get("tool") not in ("claude_code", "codex", "other"):
        probs.append("implementer.tool must be claude_code, codex or other")
    if not isinstance(doc.get("reviews"), list):
        probs.append("reviews must be a list")
    else:
        for i, r in enumerate(doc["reviews"]):
            for key, allowed in (("context", ("isolated", "shared", "unverified")), ("executed_by", ("subagent", "process", "self", "unknown")),
                                 ("attestation", ("hook_observed", "self_reported", "ci_job"))):
                if isinstance(r, dict) and r.get(key) is not None and r[key] not in allowed:
                    probs.append(f"reviews[{i}].{key} must be one of {', '.join(allowed)}")
    u2 = doc.get("u2")
    if u2 is not None:
        hits, conf, run = u2.get("confirm_scope_hits"), u2.get("human_confirmation"), u2.get("run")
        if run not in ("interactive", "non_interactive", "unknown") or conf not in ("not_required", "obtained", "not_obtained") or not isinstance(hits, list):
            probs.append("u2 needs run, human_confirmation and confirm_scope_hits")
        elif (not hits and conf != "not_required") or (hits and conf == "not_required") or (run != "interactive" and conf == "obtained"):
            probs.append("u2 is inconsistent (no hits -> not_required; obtained only in an interactive run)")
    ch = doc.get("chain")
    if ch is not None and (not SLUG.match(str(ch.get("slug", ""))) or ch.get("stage") not in ("intent", "spec", "feature")):
        probs.append("chain needs a kebab-case slug (not archive) and stage intent | spec | feature")
    return probs


def check_body(body, explainer):
    probs = []
    heads = [ln.strip() for ln, inside in fences(body.splitlines()) if not inside and ln.startswith("## ")]
    if heads == ["## Verification"]:  # refresh draft of an open PR
        return ["agent markers left (<!-- agent: ... -->): write that text or delete the line"] if MARKER.search(body) else []
    pos = [heads.index(h) if h in heads else -1 for h in ALL_TIERS]
    if -1 in pos:
        probs.append("missing heading(s): " + ", ".join(h for h, p in zip(ALL_TIERS, pos) if p == -1))
    elif pos != sorted(pos):
        probs.append("headings out of order: " + " -> ".join(ALL_TIERS))
    if explainer == "required" or "## Explainer" in heads:
        probs += [f"explainer_missing:{p}" for p in explainer_problems(body)]
    if MARKER.search(body):
        probs.append("agent markers left (<!-- agent: ... --> or %% agent:): write that text or delete the line")
    probs += block_problems(body)
    return probs


# ---------------------------------------------------------------- commands
def root_or_die():
    r = git(os.getcwd(), "rev-parse", "--show-toplevel")
    if not r:
        print("AGENTIVE_PR=stop reason=not_a_git_repo; NEXT=run inside the repository")
        sys.exit(2)
    return r


def cmd_draft(args):
    repo = Repo(root_or_die())
    for n in repo.notes:
        print(n)
    if repo.default and repo.branch in (repo.default, "HEAD"):
        print(f"AGENTIVE_PR=stop reason=on_{'default_branch' if repo.branch != 'HEAD' else 'detached_head'}; "
              "NEXT=create a work branch (git switch -c <type>/<slug>) and commit there")
        return 1
    if repo.stop:
        print(repo.stop)
        return 1
    n_notes = len(repo.notes)
    rules = repo.rules()
    for n in repo.notes[n_notes:]:
        print(n)
    chain, chain_probs = chain_link(repo)
    u2 = u2_block(repo, rules)
    tier, reasons = predict_tier(repo, rules, chain, u2)
    need = "required" if tier in (2, "uncertain") else "optional"
    out = args.out or os.path.join(repo.root, ".agents", "state", "pr-explainer.md" if args.explain else "pr-body.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open_pr = find_open_pr(repo)
    if args.explain:
        text = explainer_section(reasons if tier == 2 else []) + "\n"
    else:
        v, vs = verify_state(repo)
        if vs != "reused":
            nxt = "commit or stash the uncommitted changes, then rerun" if vs == "dirty" else \
                ("run .agents/bin/verb verify --staged (it verifies exactly the committed tree and sets untracked files aside), "
                 "then rerun sh .agents/skills/pr/scripts/pr-body.sh draft")
            print(f"AGENTIVE_PR=stop reason=verify_{vs}; NEXT={nxt}")
            return 1
        if open_pr:  # an open PR: /pr refreshes only ## Verification
            parts = [verification_section(repo, v, chain)]
        else:
            parts = ([explainer_section(reasons if tier == 2 else [])] if need == "required" else []) + \
                [verification_section(repo, v, chain), risk_section(repo, chain, args.intent_skipped), revert_section(),
                 review_block(repo, args, u2, chain)]
        text = "\n\n".join(parts) + "\n"
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for p in chain_probs:
        print(f"CHAIN_PROBLEM {p}")
    cs = f"{chain['slug']}:{chain['stage']}" if chain else "none"
    print(f"REASONS {'; '.join(reasons)}")
    mode = "explain" if args.explain else ("refresh" if open_pr else "new")
    print(f"PR_BODY={out} MODE={mode} PR={open_pr or 'none'} TIER={tier} EXPLAINER={need} CHAIN={cs} U2={u2['human_confirmation']}; "
          f"NEXT=replace every agent marker in {out}, then sh .agents/skills/pr/scripts/pr-body.sh check {out}")
    return 0


def cmd_check(args):
    body = read(args.file)
    if body is None:
        print(f"AGENTIVE_PR=stop reason=no_file; NEXT=sh .agents/skills/pr/scripts/pr-body.sh draft")
        return 2
    if args.explain_only:
        probs = [f"explainer_missing:{p}" for p in explainer_problems(body)] + \
            (["agent markers left"] if MARKER.search(body) else [])
    else:
        probs = check_body(body, args.explainer)
    for p in probs:
        print(f"PROBLEM {p}")
    if probs:
        print(f"AGENTIVE_PR=check_failed problems={len(probs)}; NEXT=fix {args.file} and rerun the check")
        return 1
    print(f"AGENTIVE_PR=check_ok file={args.file}; NEXT=sh .agents/skills/pr/scripts/pr-body.sh publish {args.file} --title \"<title>\"")
    return 0


def splice(body, heading, new_section):
    """Replace the level-2 section `heading` of body (insert it first when missing)."""
    lines = body.split("\n")
    marks = [(i, ln) for i, (ln, inside) in enumerate(fences(lines)) if not inside and ln.startswith("## ")]
    start = next((i for i, ln in marks if ln.strip() == heading), None)
    new = new_section.rstrip("\n").split("\n") + [""]
    if start is None:
        first = marks[0][0] if marks else 0
        return "\n".join(lines[:first] + new + lines[first:])
    end = next((i for i, _ in marks if i > start), len(lines))
    return "\n".join(lines[:start] + new + lines[end:])


def cmd_publish(args):
    root = root_or_die()
    repo = Repo(root, fetch=False)        # draft fetched the base already; publish needs only the names
    if not repo.default:
        print(repo.stop)
        return 1
    body = read(args.file)
    if body is None:
        print("AGENTIVE_PR=stop reason=no_file; NEXT=sh .agents/skills/pr/scripts/pr-body.sh draft")
        return 2
    slug = origin_repo(root)
    if not slug:
        print("AGENTIVE_PR=stop reason=no_github_origin; NEXT=add the GitHub remote as origin")
        return 1
    owner = slug.split("/")[0]
    if not git(root, "ls-remote", "--exit-code", "--heads", "origin", repo.branch or ""):
        print(f"AGENTIVE_PR=stop reason=branch_not_pushed; NEXT=git push -u origin {repo.branch}")
        return 1
    try:
        prs = gh_json(f"repos/{slug}/pulls?head={owner}:{repo.branch}&state=open") or []
        explain_only = section(body, "## Verification") is None
        if prs:
            n = prs[0]["number"]
            cur = (gh_json(f"repos/{slug}/pulls/{n}") or {}).get("body") or ""
            heading = "## Explainer" if explain_only else "## Verification"
            new = splice(cur, heading, heading + "\n\n" + (section(body, heading) or ""))
            tmp = args.file + ".publish"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            gh(["-X", "PATCH", f"repos/{slug}/pulls/{n}", "-F", f"body=@{tmp}"])
            print(f"PR={n} UPDATED={heading[3:]}")
            print(f"next: /pr-feedback {n}")
            return 0
        if explain_only:
            print("AGENTIVE_PR=stop reason=no_open_pr; NEXT=run /pr without --explain to open the PR")
            return 1
        probs = check_body(body, "optional")
        heads = [ln for ln, inside in fences(body.splitlines()) if not inside and ln.startswith("## ")]
        if probs or len(heads) < len(ALL_TIERS):
            print(f"AGENTIVE_PR=stop reason=body_incomplete ({'; '.join(probs) or 'refresh draft'}); "
                  "NEXT=sh .agents/skills/pr/scripts/pr-body.sh draft, fill it, check it, then publish")
            return 1
        if not args.title:
            print("AGENTIVE_PR=stop reason=no_title; NEXT=rerun publish with --title \"<title>\"")
            return 1
        res = json.loads(gh(["-X", "POST", f"repos/{slug}/pulls", "-f", f"title={args.title}", "-f", f"head={repo.branch}",
                             "-f", f"base={repo.default}", "-F", f"body=@{args.file}"]))
        print(f"PR={res.get('number')} URL={res.get('html_url')} CREATED")
        print(f"next: /pr-feedback {res.get('number')} after CI finishes")
        return 0
    except (GhError, ValueError, KeyError) as e:
        print(f"AGENTIVE_PR=stop reason=gh_api_failed ({e}); NEXT=check `gh auth status` and rerun publish")
        return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pr-body.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--explain", action="store_true")
    d.add_argument("--tool", choices=["claude_code", "codex", "other"])
    d.add_argument("--model")
    d.add_argument("--session-id")
    d.add_argument("--intent-skipped", action="store_true")
    d.add_argument("--hotfix-of", type=int)
    d.add_argument("--out")
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--explainer", choices=["required", "optional"], default="optional")
    c.add_argument("--explain-only", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("file")
    p.add_argument("--title")
    args = ap.parse_args(argv)
    return {"draft": cmd_draft, "check": cmd_check, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
