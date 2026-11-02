# agentive-unit: lib.agentive
# design: spec.work_loop.pr_skill, spec.work_loop.pr_explainer, spec.verification.E1a, spec.verification.E1b, contracts[pr-body-fw-review], spec.spec_stage.chain.ci_check.link, spec.spec_stage.triage.user_override.skip_hard_to_reverse, decisions.accepted[WFCHANGE], decisions.accepted[BASEREF], decisions.accepted[VERIFYTREE], decisions.accepted[HUMANMERGE], decisions.accepted[REQUESTLINE], decisions.accepted[REVIEWRECORD], decisions.accepted[LOCALTIER], decisions.accepted[EXPLAIN], decisions.accepted[PRLABEL], decisions.accepted[PRADOPT], spec.maintenance.janitor.label
"""PR body for the repo skill /pr (owner C; python3 stdlib only). Called by .agents/skills/pr/scripts/pr-body.sh.

  draft   [--explain] [--tool T] [--model M] [--session-id S] [--intent-skipped] [--hotfix-of N] [--request TEXT] [--out F]
          write the body skeleton: deterministic parts filled (## Verification from .agents/state/verify.json,
          ## Risk from plan.md ## Risks, the agentive-review block), `<!-- agent: ... -->` markers where the agent
          writes prose; ## Explainer first when the local rules predict tier 2 or leave the tier uncertain
  check   <file> [--explainer required|optional]   headings, Explainer presence, markers, agentive-review block
  publish <file> [--title T] [--label NAME ...] [--replace-body]   open the PR (gh api POST) or, on an open PR, replace only
          ## Verification (an --explain draft: only ## Explainer) with gh api PATCH; an open PR whose body has none of the /pr
          sections (a person opened it) gets the whole body and keeps the old text quoted under ## Original description (PRADOPT);
          each --label is added and read back before the final line
          (E2E-40: a step after /pr cannot be relied on, because in headless runs the last line ends the run)
Every run ends with one line for the agent; problems end with `NEXT=`. draft fetches origin/<default> once with an
explicit refspec first (gitgh.base_ref, BASEREF): a missing base is a stop (base_ref_missing, no_merge_base,
no_commits_ahead), never "no diff".
Tier prediction runs the CI classifier (review/pipeline.rules_estimate) on the local diff; CI (RV4, RV8) is authoritative.
`check` also runs the Explainer visual checks (explainer_style_problems); CI checks the Explainer's presence only.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

from agentive import ayaml
from agentive import verb as verbmod
from agentive.review import attest
from agentive.review import config as reviewcfg
from agentive.review import gitio as reviewgit
from agentive.review import secpaths
from agentive.gitgh import GhError, base_ref, gh, gh_json, git, no_human, origin_repo

VERBS = ["setup", "build", "lint", "typecheck", "test", "verify", "run"]
ALL_TIERS = ["## Verification", "## Risk", "## How to revert"]
SLUG = re.compile(r"^(?!archive$)[a-z0-9]+(?:-[a-z0-9]+)*$")
CHAIN_PATH = re.compile(r"^docs/plans/([^/]+)/(intent|spec|plan)\.md$")
MARKER = re.compile(r"<!-- agent:|%% agent:")
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
# Explainer visual spec (decision explainer_visual, user 2026-10-31; work_loop.pr_explainer.visual): theme colors only, one
# accent border (GitHub blue #0969da) for new and changed steps, a dashed border for removed ones, text tags NEW / CHANGED /
# REMOVED, and this legend line right under the After diagram. Before carries no classDef.
LEGEND = ("How to read: NEW / CHANGED / REMOVED name the change · thick blue border = new or changed step · dashed border = "
          "removed step · thick arrow = new call · dotted arrow = removed call · plain = unchanged")
CLASSDEFS = ["classDef added stroke:#0969da,stroke-width:3px", "classDef changed stroke:#0969da,stroke-width:3px",
             "classDef removed stroke-width:2px,stroke-dasharray:6 4"]
TAGS = {"added": "NEW", "changed": "CHANGED", "removed": "REMOVED"}
SUMMARY_HEAD = "**What changes:**"


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

    def policy(self, rel):
        """A policy file as CI reads it: from origin/<default> (the base commit), None when absent there."""
        return self.show(f"refs/remotes/origin/{self.default}", rel) if self.default else None

    def rules_text(self):
        """code_review.md from origin/<default>; when that commit has no rules block (the install PR), from the work tree,
        with a NOTE: then the PR branch's own rules decide the local estimate (CI reads the base)."""
        if getattr(self, "_rules_text", None) is None:
            src = self.policy("code_review.md")
            if not rules_block(src):
                local = read(os.path.join(self.root, "code_review.md"))
                if rules_block(local):
                    self.notes.append(f"NOTE rules from the work tree: origin/{self.default} has no code_review.md rules block "
                                      "(local estimate only; CI reads the base commit)")
                    src = local
            self._rules_text = src or ""
        return self._rules_text or None

    def rules(self):
        """The review rules block (trust boundary merged with the floor), or {}."""
        return rules_block(self.rules_text()) or {}


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


def precommit_record(repo):
    """The local review record (.agents/state/precommit-review.json) when it covers this branch's content, else {}.
    E2E-44: the file is untracked state that survives from the previous change; copying it into a new PR body would present a
    review that never saw this content (and its U2 confirmation). The record binds to the reviewed tree: it is used only when
    its `tree` (the staged tree `agentive-review precommit` reviewed; older records: reviewer.reviewed_ref) equals HEAD^{tree}.
    Any other record, or one that names no tree, is dropped with `NOTE PRECOMMIT_STALE ...; NEXT=...` (added once to repo.notes)."""
    cached = getattr(repo, "_precommit", None)
    if cached is not None:
        return cached
    rec = load_json(os.path.join(repo.root, ".agents", "state", "precommit-review.json"))
    if not isinstance(rec, dict) or not rec:
        repo._precommit = {}
        return repo._precommit
    head_tree = git(repo.root, "rev-parse", "--verify", "-q", "HEAD^{tree}") or ""
    reviewed = rec.get("tree") or (rec.get("reviewer") or {}).get("reviewed_ref") or ""
    if reviewed and reviewed == head_tree:
        repo._precommit = rec
        return rec
    repo._precommit = {}
    cover = f"tree {reviewed[:12]}" if reviewed else "no tree"
    repo.notes.append(
        f"NOTE PRECOMMIT_STALE .agents/state/precommit-review.json reviewed {cover} (head {str(rec.get('head_sha') or 'unknown')[:12]}) "
        f"but this branch's content is tree {head_tree[:12]}: the review is not copied into the PR body and a U2 confirmation in it "
        f"counts as not obtained; NEXT=run /precommit-review on this content (git reset --soft origin/{repo.default or '<default>'}, "
        "run /precommit-review, commit again), then rerun sh .agents/skills/pr/scripts/pr-body.sh draft; CI reviews the PR either way")
    return repo._precommit


def u2_block(repo, rules):
    hits = confirm_hits(repo, rules)
    rec = precommit_record(repo)
    ru = rec.get("u2") or {}
    run = ru.get("run") or ("non_interactive" if no_human() else "interactive")
    if not hits:
        conf = "not_required"
    else:
        rec_paths = {h.get("path") for h in rec.get("confirm_scope_hits") or [] if isinstance(h, dict)}
        ok = ru.get("human_confirmation") == "obtained" and run == "interactive" and set(hits) <= rec_paths
        conf = "obtained" if ok else "not_obtained"
    return {"run": run, "human_confirmation": conf, "confirm_scope_hits": hits}


def _few(items, n=3):
    items = list(items)
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


# E2E-30 rest (LOCALTIER, decision e2e_28_31): why a source file leaves the estimate open and what CI does. No deterministic rule can
# settle it safely: for such a file tier 2 comes only from what the path rules cannot see, the RV3 LLM classifier calling the change
# `security` or the RV4 raise-only check asking for a human; both read the content [design judgment].
UNCERTAIN_SOURCE = ("class undecided by rules: {files}. CI asks its LLM classifier: tier 1 unless it calls the change a security "
                    "change or the raise-only check asks for a human (then tier 2 and the Explainer is required). The Explainer is "
                    "written now to be safe")


def predict_tier(repo, rules, chain, u2):
    """(0 | 1 | 2 | 'uncertain', reasons[]) from the deterministic rules on the local diff. E2E-29 / E2E-30 (decision
    e2e_28_31): the estimate runs the CI classifier itself (review/pipeline.rules_estimate: RV1 diff reading and signals,
    RV2, RV3 rules, RV4 rules_tier) on origin/<default>...HEAD with the base's policy files, so a dependency or lockfile
    change is tier 2 (DEPHUMAN) here as in CI, a [tool.ruff] edit is config and a new test file is test. Left to CI: the LLM
    layers (a file the RV3 rules leave undecided makes the estimate uncertain and the reason says why and what CI will do, E2E-30),
    the PR author and labels, check results.
    Reasons are display strings (`code` or `code:detail`); merge_lines takes the code before the first colon."""
    if not repo.changes:
        return "uncertain", [f"empty diff: the {repo.ahead} commit(s) ahead of origin/{repo.default} change nothing in total"]
    from agentive.review import pipeline as reviewpipeline     # late: the review pipeline is needed only here
    rules_l = reviewcfg.load_rules(lambda rel: repo.rules_text() if rel == "code_review.md" else repo.policy(rel))
    try:
        est = reviewpipeline.rules_estimate(repo.root, repo.base, repo.head, repo.policy, u2, rules_l=rules_l)
    except (reviewgit.GitError, OSError, ValueError) as e:      # never a crash: CI decides
        return "uncertain", [f"local estimate failed ({str(e).splitlines()[0] if str(e) else type(e).__name__})"]
    reasons = []
    for h in est["rv2"]["hits"]:
        if h["reason"] == "check_config":
            reasons.append(f"check_config:{h['path']}" + (h["glob"][h["glob"].index("#"):] if "#" in h["glob"] else ""))
        else:
            reasons.append(f"trust_boundary:{h['reason']}:{h['path']}")
    for code, detail in est["always"]:
        if code == "always_human" and "protected_check_config" in detail:
            continue                                            # named above per path
        reasons.append(code if code == "chain_approval" else f"{code}: {detail}")
    # tier2_class from an RV2 security hit repeats that hit; it is named only when a path rule gave the class
    reasons += [r for r in est["reasons"] if r in ("U2.non_interactive", "large_change")
                or (r == "tier2_class" and not est["rv2"]["hits"])]
    if chain and chain["stage"] in ("intent", "spec") and "chain_approval" not in reasons:
        reasons.append("chain_approval")                       # the branch name names the stage (chain_link)
    reasons = list(dict.fromkeys(reasons))
    if est["tier"] == 2 or reasons:
        return 2, reasons
    if est["tier"] == "uncertain":
        return "uncertain", [UNCERTAIN_SOURCE.format(files=_few(est["undecided"]))]
    label = "tier0 classes " if est["tier"] == 0 else "classes "
    return est["tier"], [label + ",".join(est["classes"])]


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
    """The ## Explainer skeleton (work_loop.pr_explainer.contents and .visual): the agent replaces every marker."""
    why = "; ".join(reasons) if reasons else "tier uncertain from the local rules"
    return "\n".join([
        "## Explainer", "",
        f"{SUMMARY_HEAD} <!-- agent: one sentence in the PR's language: what this PR changes, as the reviewer will see it -->", "",
        "<!-- agent: 3-5 bullets: New or Changed (what, with path:line), Unchanged (what stays, with path:line), You decide "
        "(what the human must judge); cite code for every claim as path:line, path:start-end or path#symbol (head tree; removed "
        "code from the base tree). Code locations go here, never into the diagram labels -->",
        f"- Tier 2 (predicted): {why}", "",
        "### Before", "", "```mermaid", "flowchart TD",
        "  %% agent: the current flow on the base branch for the part this PR touches, only steps and calls that exist there: "
        "3-10 nodes and at most 12 arrows; one entry node; one node or one arrow per line, nodes first; entry and result as "
        "id([\"...\"]), steps as id[\"...\"]; label = a plain step starting with a verb (25 characters or fewer) then <br/> and "
        "the symbol name, e.g. check_password(); no paths, no line numbers, no colors, no classDef, no style or init lines",
        "```", "",
        "### After", "", "```mermaid", "flowchart TD",
        "  %% agent: every Before node with the same id and label, then the change: a new step gets a label starting NEW<br/> "
        "and :::added, a changed step CHANGED<br/> and :::changed, a Before step this PR deletes stays with REMOVED<br/> and "
        ":::removed; arrows: unchanged -->, new ==>, removed -.->|removed|; draw only calls that exist in the head tree (a step "
        "nothing calls yet gets an arrow label saying so); keep the three classDef lines below exactly",
        *["  " + c for c in CLASSDEFS],
        "```", "", LEGEND])


def changes_review_workflow(repo):
    """True when base...HEAD adds or changes the review workflow and the effective CI tool is claude (E2E-11)."""
    if not any(p == ".github/workflows/agentive-review.yml" and st != "D" for st, p, _, _ in repo.changes):
        return False
    def src(rel):
        return read(os.path.join(repo.root, rel))
    return reviewcfg.ci_tool(reviewcfg.pipeline_section(src), reviewcfg.load_manifest(src).value) == "claude"


REQUEST_MARKER = ('<!-- agent: Request: <the issue URL, or user request: "<what the user asked for, in one sentence and in the '
                  'user\'s words>"> (where this change was asked for; write it, never delete this line) -->')


def request_line(chain, request=None, hotfix_of=None):
    """E2E-22 (REQUESTLINE): the first line of ## Risk names where the change was asked for, so the RV4 raise-only check (a)
    can see the link between a new capability and its request. Deterministic when known (--request, a chain task, a hotfix),
    else a marker the agent replaces [design judgment: no presence check in `check`; a body without the line stays valid]."""
    if request:
        return "Request: " + " ".join(str(request).split())
    if chain:
        return f"Request: docs/plans/{chain['slug']}/ ({chain['stage']} stage of the chain task)"
    if hotfix_of:
        return f"Request: hotfix of PR #{hotfix_of}"
    return REQUEST_MARKER


def risk_section(repo, chain, intent_skipped, request=None, hotfix_of=None):
    out = ["## Risk", "", request_line(chain, request, hotfix_of), ""]
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
    rec = precommit_record(repo)      # E2E-44: only a record of this branch's tree
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
        for k in ("self_reason", "session_id", "agent_id", "agent_type", "reviewed_ref"):
            if rv.get(k):
                e[k] = rv[k]
        block["reviews"].append(e)
    block["u2"] = u2
    if chain:
        block["chain"] = chain
    if args.hotfix_of:
        block["hotfix_of"] = args.hotfix_of
    return wrap_review_record("```yaml agentive-review\n" + ayaml.dumps({"agentive-review": block}).rstrip("\n") + "\n```")


REVIEW_RECORD_HEADING = "## agentive review record"
REVIEW_RECORD_SUMMARY = "Machine-read review provenance (CI reads and updates it; do not edit)"


def wrap_review_record(fenced):
    """E2E-23 (REVIEWRECORD): the machine block gets its own heading and is folded in <details>; the blank lines after
    <summary> and before </details> let GitHub render the code block inside. Every parser finds the fenced block wherever it
    is (measured: review/util.fenced_blocks, prstate, block_problems), and CI replaces only the fence, so the wrapper stays.
    Not an HTML comment: the record stays auditable and a `-->` in a YAML value cannot break it [design judgment: names]."""
    return "\n".join([REVIEW_RECORD_HEADING, "", f"<details><summary>{REVIEW_RECORD_SUMMARY}</summary>", "", fenced, "", "</details>"])


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


def open_pr_body(repo, n):
    """The current body of open PR n ("" for an empty body), or None when it cannot be read (gh failed, no body key)."""
    slug = origin_repo(repo.root)
    try:
        pr = gh_json(f"repos/{slug}/pulls/{n}") or {}
    except (GhError, ValueError):
        return None
    return (pr.get("body") or "") if "body" in pr else None


# ---------------------------------------------------------------- a PR body that /pr did not write (PRADOPT)
PR_SECTION_HEADS = ("## Explainer", "## Verification", "## Risk", "## How to revert")
ORIGINAL_HEADING = "## Original description"


def lf(text):
    """GitHub's web editor saves a body with \r\n (Sapling's source notes it); every rule below reads \n."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def is_pr_body(text):
    """True when a PR body carries a part /pr writes: a level-2 heading ## Explainer | ## Verification | ## Risk |
    ## How to revert | ## agentive review record outside a code fence, or the agentive-review block. A person's own heading of
    one of those names counts too [design judgment: the rule stays a heading list; such a body is refreshed, not adopted]."""
    text = lf(text)
    heads = {ln.strip() for ln, inside in fences(text.split("\n")) if not inside and ln.startswith("## ")}
    return bool(heads & (set(PR_SECTION_HEADS) | {REVIEW_RECORD_HEADING})) or bool(re.search(r"(?m)^```yaml agentive-review[ \t]*$", text))


def quote_original(text):
    """Every line behind `> ` (an empty line becomes `>`), so no line of a person's text reads as a heading, a fence or a marker."""
    lines = lf(text).strip("\n").split("\n")
    return "\n".join("> " + ln if ln.strip() else ">" for ln in lines)


def quoted_original_only(text):
    """The quoted text when `text` is already nothing but an ## Original description section (a second adoption must not nest it)."""
    lines = lf(text).strip("\n").split("\n")
    marks = [i for i, (ln, inside) in enumerate(fences(lines)) if not inside and ln.startswith("## ")]
    if len(marks) == 1 and lines[marks[0]].strip() == ORIGINAL_HEADING and not "".join(lines[:marks[0]]).strip():
        return "\n".join(lines[marks[0] + 1:]).strip("\n")
    return None


def adopt_body(draft, current):
    """The whole drafted /pr body, then the old text of the PR quoted under ## Original description (last, so the Explainer
    stays the first heading: explainer_problems wants it first on tier 2). An empty old body adds no section."""
    if not lf(current).strip():
        return draft
    kept = quoted_original_only(current)
    return draft.rstrip("\n") + "\n\n" + ORIGINAL_HEADING + "\n\n" + (kept if kept is not None else quote_original(current)) + "\n"


def body_fingerprint(text):
    return hashlib.sha256(lf(text).encode("utf-8")).hexdigest()


def seen_path(root, n):
    return os.path.join(root, ".agents", "state", f"pr-body-seen-{n}.sha256")


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


# ---------------------------------------------------------------- Explainer visual checks (local /pr only)
# Decision explainer_visual (user 2026-10-31): the deterministic checks of the diagram profile run in `pr-body.sh check`
# only; CI RV8 stays presence-only (EXPLAIN option 2). String parsing of the one-statement-per-line profile, no Mermaid run.
STYLE_NEXT = ("rewrite ## Explainer by the Explainer rules of .agents/skills/pr/SKILL.md step 4 (the draft's %% agent: lines "
              "say the same), then rerun the check")
_OPEN = r"\(\[|\[\[|\[\(|\(\(|\{\{|\[|\(|\{|>"
_CLOSE = r"\]\)|\]\]|\)\]|\)\)|\}\}|\]|\)|\}"
_DECL = re.compile(r"^([A-Za-z_][\w-]*)\s*(" + _OPEN + r")(.*?)(" + _CLOSE + r")\s*(?::::([A-Za-z_][\w-]*))?$")
_ID = re.compile(r"^([A-Za-z_][\w-]*)(?::::([A-Za-z_][\w-]*))?$")
_ARROW = re.compile(r"\s*(-\.+->|=+>|-{2,}>|-\.+-|={3,}|-{3,}|--[xo])\s*(?:\|([^|]*)\|)?\s*")
_SKIP = re.compile(r"^(flowchart|graph|accTitle|accDescr|direction|click|end$|%%(?!\{))")
_PATH = re.compile(r"[\w.-]*/[\w./-]*\.[A-Za-z0-9]{1,5}\b|\b[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|rb|php|cs|"
                   r"c|h|cc|cpp|hpp|swift|scala|sh|ya?ml|json|toml|md|sql|html|css|vue|svelte|lock)\b|#|:\d+")
_BR = re.compile(r"<br\s*/?>", re.I)
MAX_LABEL = 25


def _label_lines(raw):
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return [x.strip() for x in _BR.split(text) if x.strip()]


def parse_flowchart(src):
    """The one-statement-per-line profile of a mermaid flowchart -> {nodes: {id: {label: [lines], cls}}, edges: [(a, op, b,
    label)], classdefs: [normalized lines], styled: [offending lines], subgraphs: n, marked: bool}."""
    g = {"nodes": {}, "edges": [], "classdefs": [], "styled": [], "subgraphs": 0, "class_stmt": []}
    lines = src.splitlines()
    if lines and lines[0].strip() == "---":
        g["styled"].append("frontmatter (--- ... ---)")
    in_descr = False
    for raw in lines:
        ln = raw.strip()
        if in_descr:
            in_descr = "}" not in ln
            continue
        if not ln or _SKIP.match(ln):
            in_descr = ln.startswith("accDescr") and ln.rstrip().endswith("{")
            continue
        if ln.startswith("%%{") or re.match(r"^(style|linkStyle)\s", ln) or re.search(r"\b(fill|color)\s*:", ln):
            g["styled"].append(ln)
            if not ln.startswith("classDef"):
                continue
        if ln.startswith("classDef"):
            g["classdefs"].append(" ".join(ln.split()))
            continue
        if re.match(r"^class\s", ln):
            parts = ln.split()
            for nid in (parts[1].split(",") if len(parts) > 2 else []):
                g["class_stmt"].append((nid, parts[2].rstrip(";")))
            continue
        if ln.startswith("subgraph"):
            g["subgraphs"] += 1
            continue
        ln = ln.rstrip(";")
        pieces = _ARROW.split(ln)
        if len(pieces) == 1:
            _node(g, ln)
            continue
        ends = pieces[0::3]
        for i in range(len(ends) - 1):
            a, b = [_node(g, x) for x in (ends[i], ends[i + 1])]
            for x in a:
                for y in b:
                    g["edges"].append((x, pieces[3 * i + 1], y, (pieces[3 * i + 2] or "").strip()))
    for nid, cls in g["class_stmt"]:
        if nid in g["nodes"]:
            g["nodes"][nid]["cls"] = cls
    return g


def _node(g, text):
    """Ids of `a`, `a & b`, `a:::c` or a declaration; records labels and classes."""
    out = []
    for part in text.split("&"):
        part = part.strip()
        m = _DECL.match(part)
        if m:
            n = g["nodes"].setdefault(m.group(1), {"label": [], "cls": None})
            n["label"] = _label_lines(m.group(3))
            n["cls"] = m.group(5) or n["cls"]
            n.pop("bare", None)
            out.append(m.group(1))
            continue
        m = _ID.match(part)
        if m:
            n = g["nodes"].setdefault(m.group(1), {"label": [m.group(1)], "cls": None, "bare": True})
            n["cls"] = m.group(2) or n["cls"]
            out.append(m.group(1))
    return out


def _mermaid_blocks(part):
    """[(start index, end index, source)] of the ```mermaid blocks in a list of (line, inside) pairs."""
    out, i = [], 0
    while i < len(part):
        if part[i][0].strip().startswith("```mermaid"):
            j = i + 1
            while j < len(part) and not part[j][0].strip().startswith("```"):
                j += 1
            out.append((i, j, "\n".join(ln for ln, _ in part[i + 1:j])))
            i = j + 1
        else:
            i += 1
    return out


def explainer_style_problems(body):
    """[(code, one-line message)] of the Explainer visual profile; [] when the section is missing (presence is
    explainer_problems). Codes: no_summary_line, before_has_markers, palette_not_canonical, legend_not_canonical,
    too_few_nodes, too_many_nodes, too_many_arrows, label_has_path, label_too_long, marker_without_tag, orphan_node,
    no_entry, before_ids_missing_in_after."""
    lines = fences(body.splitlines())
    h2 = [i for i, (ln, inside) in enumerate(lines) if not inside and ln.startswith("## ")]
    exp = [i for i in h2 if lines[i][0].strip() == "## Explainer"]
    if not exp:
        return []
    end = next((i for i in h2 if i > exp[0]), len(lines))
    part = lines[exp[0] + 1:end]
    b = next((i for i, (ln, inside) in enumerate(part) if not inside and ln.strip() == "### Before"), None)
    a = next((i for i, (ln, inside) in enumerate(part) if not inside and ln.strip() == "### After"), None)
    probs = []
    first = next((ln.strip() for ln, _ in part if ln.strip()), "")
    if not first.startswith(SUMMARY_HEAD):
        probs.append(("no_summary_line", f"the first line of ## Explainer must start with {SUMMARY_HEAD} and say in one "
                                         "sentence what this PR changes"))
    if b is None or a is None or b > a:
        return probs
    bb, ab = _mermaid_blocks(part[b:a]), _mermaid_blocks(part[a:])
    if not bb or not ab:
        return probs
    before, after = parse_flowchart(bb[0][2]), parse_flowchart(ab[0][2])
    after_end = a + ab[0][1]
    legend = next((ln.strip() for ln, _ in part[after_end + 1:] if ln.strip()), "")
    if legend != LEGEND:
        probs.append(("legend_not_canonical", f"the line right under the After diagram must be exactly: {LEGEND}"))
    if before["classdefs"] or before["class_stmt"] or any(n["cls"] for n in before["nodes"].values()) or \
            any(n["label"] and n["label"][0] in TAGS.values() for n in before["nodes"].values()):
        probs.append(("before_has_markers", "Before shows the base branch as it is: no classDef, no :::added/changed/removed, "
                                            "no NEW/CHANGED/REMOVED tag"))
    styled = before["styled"] + after["styled"]
    canon = [" ".join(c.split()) for c in CLASSDEFS]
    if styled or sorted(after["classdefs"]) != sorted(canon):
        what = styled[0] if styled else "classDef lines " + ("missing" if not after["classdefs"] else "changed")
        probs.append(("palette_not_canonical", f"use theme colors only: no style, linkStyle, fill, color, init or frontmatter; "
                                               f"After keeps exactly the three template classDef lines ({what[:60]})"))
    for name, g in (("Before", before), ("After", after)):
        n, e = len(g["nodes"]), len(g["edges"])
        if n < 3:
            probs.append(("too_few_nodes", f"{name} has {n} node(s); draw 3-10: the caller, the changed step and its result"))
        if n > 10:
            probs.append(("too_many_nodes", f"{name} has {n} nodes; keep 3-10: the changed part, one step before it and its result"))
        if e > 12:
            probs.append(("too_many_arrows", f"{name} has {e} arrows; keep 12 or fewer"))
        paths, longs = [], []
        for nid, nd in g["nodes"].items():
            if nd.get("bare"):
                continue
            text = [x for x in nd["label"] if x not in TAGS.values()]
            bad = next((x for x in text if _PATH.search(x)), None)
            if bad:
                paths.append(f"{nid} ('{bad[:40]}')")
            long_ = next((x for x in text if len(x) > MAX_LABEL), None)
            if long_:
                longs.append(f"{nid} ({len(long_)})")
        if paths:
            probs.append(("label_has_path", f"{name} label(s) with a path, # or :line: {_few(paths)}; name the step and the "
                                            "symbol, put code locations in the bullets above"))
        if longs:
            probs.append(("label_too_long", f"{name} label line(s) over {MAX_LABEL} characters: {_few(longs)}; shorten or "
                                            "split with <br/>"))
        linked = {x for x, _, y, _ in g["edges"]} | {y for x, _, y, _ in g["edges"]}
        orphans = sorted(set(g["nodes"]) - linked)
        if orphans and n > 1:
            probs.append(("orphan_node", f"{name} node(s) {', '.join(orphans)} have no arrow; connect them or drop them"))
        targets = {y for _, _, y, _ in g["edges"]}
        entries = sorted(x for x in g["nodes"] if x not in targets and x not in orphans)
        if g["edges"] and (not entries or len(entries) > max(1, g["subgraphs"])):
            probs.append(("no_entry", f"{name} has {len(entries)} entry node(s) ({', '.join(entries) or 'none'}); start each flow "
                                      "at one entry node (two flows: one titled subgraph each)"))
    untagged = []
    for nid, nd in after["nodes"].items():
        tag = nd["label"][0] if nd["label"] and not nd.get("bare") else None
        want = TAGS.get(nd["cls"] or "")
        if want and tag != want:
            untagged.append(f"{nid} (:::{nd['cls']} needs {want}<br/> first)")
        elif tag in TAGS.values() and want != tag:
            untagged.append(f"{nid} ({tag} needs :::{next(k for k, v in TAGS.items() if v == tag)})")
    if untagged:
        probs.append(("marker_without_tag", f"After node(s) {_few(untagged)}: a marked node starts its label with its tag"))
    missing = sorted(set(before["nodes"]) - set(after["nodes"]))
    if missing:
        probs.append(("before_ids_missing_in_after", f"After lacks Before node(s) {', '.join(missing)}: keep every Before node "
                                                     "with the same id (a deleted one as REMOVED with :::removed)"))
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


def style_lines(body):
    """PROBLEM texts of the Explainer visual checks (local only): `explainer_style:<code>: <message>`. Skipped while agent
    markers are left in the section (the skeleton is not a diagram yet)."""
    sec = section(body, "## Explainer") or ""
    if MARKER.search(sec):
        return []
    return [f"explainer_style:{c}: {m}" for c, m in explainer_style_problems(body)]


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
        missing = explainer_problems(body)
        probs += [f"explainer_missing:{p}" for p in missing]
        if not missing:
            probs += style_lines(body)
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
    n_printed = len(repo.notes)
    chain, chain_probs = chain_link(repo)
    u2 = u2_block(repo, rules)
    tier, reasons = predict_tier(repo, rules, chain, u2)
    need = "required" if tier in (2, "uncertain") else "optional"
    out = args.out or os.path.join(repo.root, ".agents", "state", "pr-explainer.md" if args.explain else "pr-body.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open_pr = find_open_pr(repo)
    adopt = None                    # PRADOPT: the body of an open PR that has none of the /pr sections
    if open_pr and not args.explain:
        seen = open_pr_body(repo, open_pr)
        adopt = seen if seen is not None and not is_pr_body(seen) else None
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
        if open_pr and adopt is None:  # an open PR made by /pr: /pr refreshes only ## Verification
            parts = [verification_section(repo, v, chain)]
        else:
            parts = ([explainer_section(reasons if tier == 2 else [])] if need == "required" else []) + \
                [verification_section(repo, v, chain), risk_section(repo, chain, args.intent_skipped, args.request, args.hotfix_of), revert_section(),
                 review_block(repo, args, u2, chain)]
        text = "\n\n".join(parts) + "\n"
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if open_pr and not args.explain:    # the body this draft was made against: publish stops when a person edited it since
        os.makedirs(os.path.dirname(seen_path(repo.root, open_pr)), exist_ok=True)
        if adopt is not None:
            with open(seen_path(repo.root, open_pr), "w", encoding="utf-8", newline="\n") as f:
                f.write(body_fingerprint(adopt) + "\n")
        elif os.path.exists(seen_path(repo.root, open_pr)):
            os.remove(seen_path(repo.root, open_pr))
    if adopt is not None:
        print(f"NOTE adopt pr={open_pr}: its body has none of the /pr sections (a person opened it), so this draft is the whole body; "
              f"publish keeps the old text quoted under {ORIGINAL_HEADING} and saves it to .agents/state/pr-body-before-{open_pr}.md")
    for n in repo.notes[n_printed:]:      # E2E-44: NOTE PRECOMMIT_STALE (before the final NEXT line)
        print(n)
    for p in chain_probs:
        print(f"CHAIN_PROBLEM {p}")
    cs = f"{chain['slug']}:{chain['stage']}" if chain else "none"
    print(f"REASONS {'; '.join(reasons)}")
    mode = "explain" if args.explain else ("adopt" if adopt is not None else ("refresh" if open_pr else "new"))
    print(f"PR_BODY={out} MODE={mode} PR={open_pr or 'none'} TIER={tier} EXPLAINER={need} CHAIN={cs} U2={u2['human_confirmation']}; "
          f"NEXT=replace every agent marker in {out}, then sh .agents/skills/pr/scripts/pr-body.sh check {out}")
    return 0


def cmd_check(args):
    body = read(args.file)
    if body is None:
        print(f"AGENTIVE_PR=stop reason=no_file; NEXT=sh .agents/skills/pr/scripts/pr-body.sh draft")
        return 2
    if args.explain_only:
        missing = explainer_problems(body)
        probs = [f"explainer_missing:{p}" for p in missing] + ([] if missing else style_lines(body)) + \
            (["agent markers left"] if MARKER.search(body) else [])
    else:
        probs = check_body(body, args.explainer)
    for p in probs:
        print(f"PROBLEM {p}")
    if probs:
        nxt = STYLE_NEXT if all(p.startswith("explainer_style:") for p in probs) else f"fix {args.file} and rerun the check"
        print(f"AGENTIVE_PR=check_failed problems={len(probs)}; NEXT={nxt}")
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


def draft_only_heads(body):
    """Level-2 headings of a draft other than ## Verification; empty for the refresh draft of an open PR (check_body's shape)."""
    heads = [ln.strip() for ln, inside in fences(body.splitlines()) if not inside and ln.startswith("## ")]
    return [h for h in heads if h != "## Verification"]


def human_merge(repo):
    """(bool, reasons) - True when a human must review and merge this PR (E2E-14, decisions.accepted[HUMANMERGE]): the local
    tier estimate on the local refs is 2 (the install PR, a workflow change, a trust-boundary or check-config path, a chain
    intent or spec PR, an unconfirmed U2 hit) or the PR changes the review workflow (WFCHANGE). CI (RV4, RV8) stays
    authoritative; an uncertain estimate is not a human merge here [design judgment]."""
    if not repo.base:
        return False, []
    rules = repo.rules()
    chain, _ = chain_link(repo)
    tier, reasons = predict_tier(repo, rules, chain, u2_block(repo, rules))
    if tier == 2:
        return True, reasons
    if changes_review_workflow(repo):
        return True, ["workflow_change"]
    return False, reasons


LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$")


def apply_labels(slug, n, labels):
    """E2E-40 (PRLABEL): add each label to PR n through REST and read the labels back (same logic as the janitor's label-pr.sh:
    POST, GET, one repeated POST when the label is missing). Returns (carried, missing) as lists of names."""
    def has(label):
        try:
            names = gh(["-X", "GET", f"repos/{slug}/issues/{n}/labels", "--jq", ".[].name"])
        except GhError:
            return False
        return label in names.splitlines()

    def add(label):
        try:
            gh(["-X", "POST", f"repos/{slug}/issues/{n}/labels", "-f", f"labels[]={label}"])
        except GhError:
            pass

    carried, missing = [], []
    for label in labels:
        add(label)
        if not has(label):
            add(label)
        (carried if has(label) else missing).append(label)
    return carried, missing


def label_fail_line(n, url, missing):
    names = ",".join(missing)
    kind = missing[0].split(":", 1)[1] if missing[0].startswith("agentive-janitor:") else "<kind>"
    return (f"AGENTIVE_FAIL tool=pr-label reason=label_missing pr={n} url={url} labels=missing:{names}; "
            f"NEXT=check gh auth status (the token needs write access to issues; not GITHUB_TOKEN), then add the label: "
            f"sh .agents/skills/janitor/scripts/label-pr.sh {kind} {n} (a janitor label) or gh api -X POST repos/<owner>/<repo>/issues/{n}/labels "
            f"-f 'labels[]={missing[0]}'; until then the PR is not labeled: say so in the report, then report the PR URL and end the run")


def merge_lines(n, url, human, reasons, nxt, labels=None):
    """The lines after CREATED / UPDATED. A human merge ends with MERGE=human; with no human present (headless runs
    (`claude -p`), Routines runs) the last line is AGENTIVE_PR=done ... waiting=human_merge and the run ends. `labels` (the
    names --label added and verified) goes into that final line: `labels=<names>`."""
    if not human:
        return [nxt]
    why = ",".join(dict.fromkeys(r.split(":")[0] for r in reasons)) or "tier2"
    out = [f"MERGE=human pr={n} url={url} why={why}; NEXT=tell the human to review and merge {url}; "
           "agents never merge it (guard gh-pr-merge)"]
    if no_human():
        tag = f" labels={','.join(labels)}" if labels else ""
        out.append(f"AGENTIVE_PR=done pr={n} waiting=human_merge url={url}{tag}; NEXT=report the PR URL and end the run "
                   "(a human reviews and merges it; nothing in this run waits for the merge)")
    else:
        out.append(nxt + " (fixes only; the merge stays with the human)")
    return out


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
    labels = list(dict.fromkeys(args.label or []))
    bad = [x for x in labels if not LABEL_RE.match(x)]
    if bad:
        print(f"AGENTIVE_PR=stop reason=bad_label ({bad[0]}); NEXT=pass --label with letters, digits and : . _ / - only, for example agentive-janitor:<kind>")
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
            pr = gh_json(f"repos/{slug}/pulls/{n}") or {}
            cur = pr.get("body") or ""
            heading = "## Explainer" if explain_only else "## Verification"
            full = not explain_only and bool(draft_only_heads(body))
            saved = os.path.join(root, ".agents", "state", f"pr-body-before-{n}.md")
            saved_rel = f".agents/state/pr-body-before-{n}.md"
            adopted = False
            if full and args.replace_body:
                new, what = body, "body"                  # E2E-50: an explicit replacement; the old body is saved below
            elif full and is_pr_body(cur):
                # E2E-50: a draft made in MODE=new (Explainer, Risk, review record) must not lose them to a refresh of ## Verification
                # because a PR for this branch exists now (opened by another run after the draft) and its body is already a /pr body.
                print(f"AGENTIVE_PR=stop reason=pr_opened_since_draft pr={n}; NEXT=the draft in {args.file} is a full PR body (MODE=new) but PR #{n} is open "
                      f"for this branch and already holds a /pr body, and a refresh would drop the drafted Explainer, Risk and review record: rerun "
                      f"sh .agents/skills/pr/scripts/pr-body.sh draft (it refreshes ## Verification), or rerun publish with --replace-body to write the "
                      f"whole drafted body to PR #{n} (the body it has now is saved to {saved_rel})")
                return 1
            elif full:
                # PRADOPT: a person's body (none of the /pr sections) is kept, quoted, under ## Original description
                before = seen_path(root, n)
                was = (read(before) or "").strip()
                if was and was != body_fingerprint(cur):
                    print(f"AGENTIVE_PR=stop reason=pr_body_changed_since_draft pr={n}; NEXT=PR #{n} was edited after the draft in {args.file} was made, "
                          f"so nothing was written: rerun sh .agents/skills/pr/scripts/pr-body.sh draft (it reads the body again), fill and check the "
                          f"new draft, then publish")
                    return 1
                new, what, adopted = adopt_body(body, cur), "body", True
            elif not explain_only and not is_pr_body(cur):
                print(f"AGENTIVE_PR=stop reason=pr_body_not_agentive pr={n}; NEXT=PR #{n} has a body with none of the /pr sections, and the draft in "
                      f"{args.file} holds only ## Verification: rerun sh .agents/skills/pr/scripts/pr-body.sh draft (for this PR it writes the whole "
                      f"body and keeps the old text under {ORIGINAL_HEADING}), fill and check it, then publish")
                return 1
            else:
                new, what = splice(cur, heading, heading + "\n\n" + (section(body, heading) or "")), heading[3:]
            if full:
                os.makedirs(os.path.dirname(saved), exist_ok=True)
                with open(saved, "w", encoding="utf-8", newline="\n") as f:
                    f.write(cur)
            tmp = args.file + ".publish"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            gh(["-X", "PATCH", f"repos/{slug}/pulls/{n}", "-F", f"body=@{tmp}"])
            print(f"PR={n} UPDATED={what}" + (f" saved={saved_rel}" if full else "") + (" original=kept" if adopted else ""))
            if adopted and os.path.exists(seen_path(root, n)):
                os.remove(seen_path(root, n))
            url = pr.get("html_url") or prs[0].get("html_url")
            carried, missing = apply_labels(slug, n, labels)
            if carried:
                print(f"LABELED {','.join(carried)} pr={n}")
            human, reasons = human_merge(repo)
            for ln in merge_lines(n, url, human, reasons, f"next: /pr-feedback {n}", carried):
                print(ln)
            if missing:
                print(label_fail_line(n, url, missing))
                return 2
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
        carried, missing = apply_labels(slug, res.get("number"), labels)
        if carried:
            print(f"LABELED {','.join(carried)} pr={res.get('number')}")
        human, reasons = human_merge(repo)
        for ln in merge_lines(res.get("number"), res.get("html_url"), human, reasons,
                              f"next: /pr-feedback {res.get('number')} after CI finishes", carried):
            print(ln)
        if missing:
            print(label_fail_line(res.get("number"), res.get("html_url"), missing))
            return 2
        return 0
    except (GhError, ValueError, KeyError) as e:
        print(f"AGENTIVE_PR=stop reason=gh_api_failed ({e}); NEXT=check `gh auth status` and rerun publish")
        return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pr-body.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--explain", action="store_true")
    d.add_argument("--tool", type=attest.tool_arg, choices=list(attest.TOOLS))
    d.add_argument("--model")
    d.add_argument("--session-id")
    d.add_argument("--intent-skipped", action="store_true")
    d.add_argument("--hotfix-of", type=int)
    d.add_argument("--request", help="where the change was asked for: an issue URL or the user's request in one sentence (E2E-22)")
    d.add_argument("--out")
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--explainer", choices=["required", "optional"], default="optional")
    c.add_argument("--explain-only", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("file")
    p.add_argument("--title")
    p.add_argument("--replace-body", action="store_true",
                   help="an open PR exists but the draft is a full body (MODE=new): write the whole draft as its body, old body saved, no "
                        "## Original description (E2E-50; a person's body is kept quoted without this flag, PRADOPT)")
    p.add_argument("--label", action="append", metavar="NAME",
                   help="label to add and verify before the final line; repeatable (E2E-40, PRLABEL)")
    args = ap.parse_args(argv)
    return {"draft": cmd_draft, "check": cmd_check, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
