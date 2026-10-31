# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV1], spec.review_pipeline.criteria_scope, spec.spec_stage.chain.ci_check, spec.one_way.module (owner D)
"""RV1 deterministic preprocess (llm: false).

Input: a gitio.Diff, the agentive manifest, the effective review config, the rubric shared_globs, the check
results, the criteria tree (paths of the tree whose criteria files apply) and the PR body chain block.
Output: the review-report `rv1` object plus private fields (prefixed `_`) the later stages use.

Detection choices the design leaves open, all [design judgment]:
- tests: agentive manifest paths.tests, else common test-file name patterns (stack-agnostic list below);
- public API: removed lines in paths.public_api files that look like an exported declaration;
- new direct dependencies: the dependency lists of package files (package.json, requirements*.txt, pyproject.toml,
  go.mod, Cargo.toml, pom.xml, build.gradle[.kts]) compared between base and head;
- env vars: keys of .env.example-type files plus names read through common environment lookups in added or removed
  lines that do not occur in the other tree;
- history: commits on the base's first-parent history (one `git log --first-parent --name-only` call, E2E-13) whose
  message is a revert ('This reverts commit', 'Revert "') or names a hotfix, within 90 days of the base commit time (a
  merge commit counts through its message: a merged revert PR carries the 'Revert "' title, the generic 'Merge pull
  request #N from <branch>' subject alone is no incident); churn (rubric@1) = any commit within 30 days of it; production
  churn (rubric@2) = the most changes landed on the base branch within 90 days on one changed production file: every
  first-parent record counts 1, merge commits included (a merged PR, a squash commit or a direct commit counts 1; a
  rebase-merged PR counts one per commit; E2E-13 revised 2026-10-31 [user]). The base commit time is 'now' so a re-run
  of the same PR gives the same answer, which the reviewer eval needs;
- production files (rubric@2, E2E-13): changed files that are not tests (is_test), not docs (rv3 docs rule), not lock
  files and not manifest generated / vendor / RV1 exclude_globs paths; production modules and the production shared path
  are computed over them only.
"""
import json
import os
import posixpath
import re

from . import config as cfgmod
from . import gitio
from . import util

LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
             "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock", "go.sum", "Cargo.lock", "Gemfile.lock",
             "composer.lock", "gradle.lockfile", "packages.lock.json", "mix.lock", "pubspec.lock", "flake.lock"}
TEST_GLOBS = ["**/test_*.py", "**/*_test.py", "**/tests/**", "**/test/**", "**/__tests__/**", "**/*_test.go",
              "**/*.test.*", "**/*.spec.*", "**/*Test.java", "**/*Tests.java", "**/*IT.java", "**/*Test.kt",
              "**/src/test/**", "**/spec/**", "**/conftest.py"]
SUPPRESSIONS = [("eslint-disable", re.compile(r"eslint-disable")), ("# noqa", re.compile(r"#\s*noqa\b")),
                ("@ts-ignore", re.compile(r"@ts-ignore\b")), ("# type: ignore", re.compile(r"#\s*type:\s*ignore\b")),
                ("#nosec", re.compile(r"#\s*nosec\b")), ("nolint", re.compile(r"\bnolint\b"))]
CHECK_TAG = re.compile(r"agentive-check:\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
ENV_LOOKUPS = [re.compile(p) for p in (
    r"process\.env\.([A-Z_][A-Z0-9_]*)", r"process\.env\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]",
    r"os\.environ\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]", r"os\.environ\.get\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]",
    r"os\.getenv\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]", r"os\.Getenv\(\s*\"([A-Z_][A-Z0-9_]*)\"",
    r"os\.LookupEnv\(\s*\"([A-Z_][A-Z0-9_]*)\"", r"System\.getenv\(\s*\"([A-Z_][A-Z0-9_]*)\"",
    r"env::var\(\s*\"([A-Z_][A-Z0-9_]*)\"", r"ENV\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]",
    r"import\.meta\.env\.([A-Z_][A-Z0-9_]*)")]
ENV_FILE_GLOBS = ["**/.env.example", "**/.env.sample", "**/.env.template", "**/.env.dist", "**/env.example"]
EXPORT_DECL = re.compile(
    r"^\s*(export\s+(default\s+)?(async\s+)?(function|class|const|let|var|interface|type|enum)\b"
    r"|pub\s+(async\s+)?(fn|struct|enum|trait|type|const|mod)\b"
    r"|func\s+(\([^)]*\)\s*)?[A-Z]\w*\s*\("
    r"|type\s+[A-Z]\w*\s"
    r"|public\s+[\w<>\[\],\s]*\(|public\s+(static\s+)?(final\s+)?(class|interface|enum|record)\b"
    r"|def\s+[A-Za-z]\w*\s*\(|class\s+[A-Za-z]\w*)")
REVERT_MSG = re.compile(r"This reverts commit [0-9a-f]{7,40}|^Revert \"", re.M)
HOTFIX_MSG = re.compile(r"\bhot[- ]?fix\b", re.I)
DAY = 86400


def is_test(path, test_globs):
    return util.first_match(test_globs, path) is not None


def test_globs_for(manifest):
    t = [cfgmod.path_glob(p) for p in cfgmod.manifest_paths(manifest, "tests")]
    return t + TEST_GLOBS


def exclusion_globs(manifest, cfg):
    g = [cfgmod.path_glob(p) for p in cfgmod.manifest_paths(manifest, "generated") + cfgmod.manifest_paths(manifest, "vendor")]
    g += list(((cfg.get("stages") or {}).get("RV1") or {}).get("exclude_globs") or [])
    return g


def is_lockfile(path):
    return posixpath.basename(path) in LOCKFILES


def module_of(path, manifest):
    """one_way.module: a top-level directory under paths.src (or under the repo root when paths.src is empty)."""
    for src in cfgmod.manifest_paths(manifest, "src"):
        s = src.strip()
        while s.startswith("./"):
            s = s[2:]
        s = s.rstrip("/")
        if s in ("", "."):
            continue
        if s and (path == s or path.startswith(s + "/")):
            rest = path[len(s) + 1:]
            return s + "/" + rest.split("/")[0] if "/" in rest else s
    return path.split("/")[0] if "/" in path else "."


# ---------------------------------------------------------------- criteria map (criteria_scope)
def criteria_map(changed, tree_files, rules_file, fallback_files):
    """[{kind, sources?, files}] with each changed file in exactly one entry (ancestor_only, one_kind_per_file)."""
    tree = set(tree_files)
    rules_dir = posixpath.dirname(rules_file)
    fallbacks = sorted(p for p in tree if posixpath.basename(p) in fallback_files)
    groups = {}
    for f in changed:
        d = posixpath.dirname(f)
        if rules_file in tree and (rules_dir == "" or d == rules_dir or d.startswith(rules_dir + "/")):
            key = ("dedicated", (rules_file,))
        else:
            srcs = tuple(p for p in fallbacks
                         if posixpath.dirname(p) == "" or d == posixpath.dirname(p) or d.startswith(posixpath.dirname(p) + "/"))
            key = ("fallback", srcs) if srcs else ("none", ())
        groups.setdefault(key, []).append(f)
    out = []
    for (kind, srcs), files in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        e = {"kind": kind, "files": sorted(files)}
        if kind != "none":
            e["sources"] = list(srcs)
        out.append(e)
    return out


# ---------------------------------------------------------------- dependency lists of package files
def _deps_package_json(text):
    try:
        d = json.loads(text)
    except ValueError:
        return set()
    out = set()
    for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(d.get(k), dict):
            out |= {f"npm:{n}" for n in d[k]}
    return out


def _deps_requirements(text):
    out = set()
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln or ln.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", ln)
        if m:
            out.add("pypi:" + m.group(1).lower().replace("_", "-"))
    return out


def _deps_pyproject(text):
    out = set()
    for block in re.findall(r"(?ms)^\s*(?:dependencies|[a-z0-9_-]+)\s*=\s*\[(.*?)\]", text):
        for s in re.findall(r"['\"]([A-Za-z0-9][A-Za-z0-9._-]*)", block):
            out.add("pypi:" + s.lower().replace("_", "-"))
    sec = None
    for ln in text.splitlines():
        m = re.match(r"^\s*\[(.+?)\]\s*$", ln)
        if m:
            sec = m.group(1)
            continue
        if sec and re.match(r"tool\.poetry(\.group\.[^.]+)?\.(dev-)?dependencies$", sec):
            m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", ln)
            if m and m.group(1).lower() != "python":
                out.add("pypi:" + m.group(1).lower().replace("_", "-"))
    return out


def _deps_go_mod(text):
    out, in_block = set(), False
    for ln in text.splitlines():
        s = ln.split("//", 1)[0].strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        m = re.match(r"^(?:require\s+)?([\w.\-/~]+\.[\w.\-/~]+)\s+v\S+", s) if (in_block or s.startswith("require ")) else None
        if m and "// indirect" not in ln:
            out.add("go:" + m.group(1))
    return out


def _deps_cargo(text):
    out, sec = set(), None
    for ln in text.splitlines():
        m = re.match(r"^\s*\[(.+?)\]\s*$", ln)
        if m:
            sec = m.group(1)
            continue
        if sec and re.match(r"^(dev-|build-)?dependencies$", sec):
            m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", ln)
            if m:
                out.add("cargo:" + m.group(1))
    return out


def _deps_pom(text):
    out = set()
    for blk in re.findall(r"(?s)<dependency>(.*?)</dependency>", text):
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", blk)
        a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", blk)
        if a:
            out.add("maven:" + (g.group(1) + ":" if g else "") + a.group(1))
    return out


def _deps_gradle(text):
    out = set()
    for m in re.finditer(r"(?m)^\s*(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|kapt)\s*\(?\s*['\"]([^:'\"]+):([^:'\"]+)", text):
        out.add(f"maven:{m.group(1)}:{m.group(2)}")
    return out


PACKAGE_FILES = [("package.json", _deps_package_json), ("pyproject.toml", _deps_pyproject),
                 ("go.mod", _deps_go_mod), ("Cargo.toml", _deps_cargo), ("pom.xml", _deps_pom),
                 ("build.gradle", _deps_gradle), ("build.gradle.kts", _deps_gradle)]


def package_parser(path):
    b = posixpath.basename(path)
    for name, fn in PACKAGE_FILES:
        if b == name:
            return fn
    if re.match(r"^requirements[\w.-]*\.(txt|in)$", b):
        return _deps_requirements
    return None


def is_package_file(path):
    return package_parser(path) is not None


# ---------------------------------------------------------------- signals
def _env_names(lines):
    out = set()
    for _, text in lines:
        for rx in ENV_LOOKUPS:
            out |= set(rx.findall(text))
    return out


def _env_file_keys(text):
    out = set()
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", ln)
        if m:
            out.add(m.group(1))
    return out


def production_files(changed, test_globs, excluded_fn=None):
    """Changed paths that count as production code for rubric@2 (E2E-13): not a test, not a doc, not excluded."""
    from . import rv3 as rv3mod        # rv3 imports this module: import late
    return [p for p in changed if not is_test(p, test_globs) and not rv3mod._is_doc(p) and not (excluded_fn and excluded_fn(p))]


def signals(diff, statuses, hunks, manifest, shared_globs, test_globs, history_rev, verbs_pending, excluded_fn=None):
    repo = diff.repo
    changed = [p for _, p, _ in statuses]
    deleted = [p for st, p, _ in statuses if st == "D"]
    s = {}
    # public API change
    pub = [cfgmod.path_glob(p) for p in cfgmod.manifest_paths(manifest, "public_api")]
    if not pub:
        s["public_api_change"] = "skipped"
    else:
        breaking = False
        for p in changed:
            if util.first_match(pub, p) and any(EXPORT_DECL.match(t) for _, t in (hunks.get(p) or {}).get("removed", [])):
                breaking = True
        for st, p, _ in statuses:
            if st == "D" and util.first_match(pub, p):
                breaking = True
        s["public_api_change"] = "breaking" if breaking else "none"
    # test-only deletion: deleted tests whose production code remains
    del_tests = [p for p in deleted if is_test(p, test_globs)]
    prod_deleted = {_stem(p) for p in deleted if not is_test(p, test_globs)}
    s["test_only_deletion"] = any(_stem(p) not in prod_deleted for p in del_tests)
    # inline suppressions added
    sup = []
    for p in changed:
        for ln, text in (hunks.get(p) or {}).get("added", []):
            for name, rx in SUPPRESSIONS:
                if rx.search(text):
                    sup.append(f"{p}:{ln} {name}")
    s["inline_suppressions"] = sup
    # agentive-check: tags
    added_tags, removed_tags = set(), set()
    for p in changed:
        h = hunks.get(p) or {}
        for _, t in h.get("added", []):
            added_tags |= set(CHECK_TAG.findall(t))
        for _, t in h.get("removed", []):
            removed_tags |= set(CHECK_TAG.findall(t))
    s["agentive_check_tags"] = {"added": sorted(added_tags - removed_tags), "removed": sorted(removed_tags - added_tags)}
    # new direct dependencies
    new_deps = set()
    for st, p, old in statuses:
        fn = package_parser(p)
        if not fn or st == "D":
            continue
        head = diff.show_head(p) or ""
        base = diff.show_base(old or p) or ""
        new_deps |= fn(head) - fn(base)
    s["new_direct_dependencies"] = sorted(new_deps)
    # env vars added or removed
    env = set()
    for st, p, old in statuses:
        if util.first_match(ENV_FILE_GLOBS, p):
            env |= _env_file_keys(diff.show_head(p)) ^ _env_file_keys(diff.show_base(old or p))
            continue
        h = hunks.get(p) or {}
        added, removed = _env_names(h.get("added", [])), _env_names(h.get("removed", []))
        for n in added - removed:
            if not gitio.grep_word(repo, diff.base, n):
                env.add(n)
        for n in removed - added:
            if not gitio.grep_word(repo, diff.head, n):
                env.add(n)
    s["env_vars_changed"] = sorted(env)
    s["changed_files"] = len(changed)
    mods = sorted({module_of(p, manifest) for p in changed})
    s["modules_touched"] = mods
    s["shared_path_touched"] = any(util.first_match(shared_globs, p) for p in changed)
    # rubric@2 (E2E-13): the same two signals over production files only
    prod = production_files(changed, test_globs, excluded_fn)
    s["production_modules_touched"] = sorted({module_of(p, manifest) for p in prod})
    s["production_shared_path_touched"] = any(util.first_match(shared_globs, p) for p in prod)
    # history (reference time = base commit time); one git call on the base's first-parent history
    touched = [old or p for st, p, old in statuses if st != "A"]
    prod_set = set(prod)
    prod_touched = {old or p for st, p, old in statuses if st != "A" and p in prod_set}
    now = gitio.commit_time(repo, history_rev) if history_rev else None
    s["production_churn_commits_90d"] = 0
    if now is None or not touched:
        s["incident_history_90d"], s["recent_churn_30d"] = False, False
    else:
        hist90 = gitio.log_first_parent(repo, history_rev, touched, now - 90 * DAY, now)
        s["incident_history_90d"] = any(REVERT_MSG.search(subj + "\n" + body) or HOTFIX_MSG.search(subj) for _, _, _, subj, body, _ in hist90)
        s["recent_churn_30d"] = any(t > now - 30 * DAY for _, _, t, _, _, _ in hist90)
        per_file = {}
        for _, _, _, _, _, files in hist90:      # one landing = 1, merge commits included (a merge lists its PR's files)
            for f in files:
                if f in prod_touched:
                    per_file[f] = per_file.get(f, 0) + 1
        s["production_churn_commits_90d"] = max(per_file.values(), default=0)
    s["pending_verbs"] = list(verbs_pending)
    return s


def _stem(path):
    b = posixpath.basename(path)
    b = re.sub(r"\.(test|spec)(?=\.)", "", b)
    b = re.sub(r"^test_|_test(?=\.)|(Test|Tests|IT)(?=\.)", "", b)
    return b.split(".")[0].lower()


# ---------------------------------------------------------------- coverage (gate-only condition)
def coverage(diff, hunks, manifest, cfg, excluded_fn):
    rep = (((manifest.get("verbs") or {}).get("test") or {}).get("coverage_report"))
    c = ((cfg.get("stages") or {}).get("RV8") or {}).get("coverage") or {}
    min_pct, exempt = c.get("min_new_code_pct", 80), c.get("exempt_new_lines_lt", 20)
    new_lines = {p: {ln for ln, _ in h.get("added", [])} for p, h in hunks.items() if not excluded_fn(p)}
    n = sum(len(v) for v in new_lines.values())
    if n < exempt:
        return {"status": "exempt", "new_lines": n}
    if not rep or not os.path.isfile(os.path.join(diff.repo, rep)):
        return {"status": "coverage_unavailable", "new_lines": n}
    cov = _read_coverage(os.path.join(diff.repo, rep), diff.repo)
    if cov is None:
        return {"status": "coverage_unavailable", "new_lines": n}
    measured = hit = 0
    for p, lines in new_lines.items():
        fc = cov.get(p)
        if not fc:
            continue
        for ln in lines:
            if ln in fc:
                measured += 1
                hit += 1 if fc[ln] > 0 else 0
    if measured == 0:
        return {"status": "coverage_unavailable", "new_lines": n}
    pct = round(100.0 * hit / measured, 1)
    return {"status": "ok" if pct >= min_pct else "below_threshold", "new_code_pct": pct, "new_lines": n}


def _read_coverage(path, repo):
    """{repo-relative path: {line: hits}} from an lcov or Cobertura XML report; None for other formats."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    out = {}
    if "SF:" in text and "DA:" in text:
        cur = None
        for ln in text.splitlines():
            if ln.startswith("SF:"):
                cur = _rel(ln[3:].strip(), repo)
                out.setdefault(cur, {})
            elif ln.startswith("DA:") and cur:
                parts = ln[3:].split(",")
                try:
                    out[cur][int(parts[0])] = int(parts[1])
                except (ValueError, IndexError):
                    pass
        return out
    if "<coverage" in text and "<class " in text:
        for cls in re.finditer(r"<class\b[^>]*filename=\"([^\"]+)\"[^>]*>(.*?)</class>", text, re.S):
            f = _rel(cls.group(1), repo)
            d = out.setdefault(f, {})
            for m in re.finditer(r"<line\b[^>]*number=\"(\d+)\"[^>]*hits=\"(\d+)\"", cls.group(2)):
                d[int(m.group(1))] = int(m.group(2))
        return out
    return None


def _rel(p, repo):
    p = p.replace("\\", "/")
    r = repo.rstrip("/") + "/"
    return p[len(r):] if p.startswith(r) else p.lstrip("./")


# ---------------------------------------------------------------- chain check (spec_stage.chain.ci_check)
ARTIFACT = re.compile(r"^docs/plans/(?!archive/)([^/]+)/(intent|spec|plan)\.md$")
SLUG = re.compile(r"^(?!archive$)[a-z0-9]+(?:-[a-z0-9]+)*$")
NEXT = {
    "intent_not_on_base": "merge the intent PR (intent/{slug}) first, then run /chain-spec {slug}",
    "spec_not_on_base": "merge the spec PR (spec/{slug}) first, then run /chain-plan {slug}",
    "plan_not_in_head": "commit docs/plans/{slug}/plan.md as the first commit of feature/{slug} (/chain-plan {slug})",
    "mixed_artifacts": "split the PR: one artifact per PR",
    "multiple_slugs": "split the PR: one slug per PR",
}


def chain_check(diff, statuses, body_chain):
    arts = {}
    for st, p, _ in statuses:
        m = ARTIFACT.match(p)
        if m and st in "AMR":
            arts.setdefault(m.group(1), []).append((m.group(2), st, p))
    slugs = set(arts)
    if body_chain and SLUG.match(str(body_chain.get("slug", ""))):
        slugs.add(body_chain["slug"])
    if not slugs:
        return {"result": "not_chain"}
    problems = []
    if len(slugs) > 1:
        problems.append("multiple_slugs")
    slug = (body_chain or {}).get("slug") if (body_chain or {}).get("slug") in slugs else sorted(slugs)[0]
    kinds = {k for k, _, _ in arts.get(slug, [])}
    stage = (body_chain or {}).get("stage") if (body_chain or {}).get("slug") == slug else None
    if stage not in ("intent", "spec", "feature"):
        stage = "intent" if "intent" in kinds else "spec" if "spec" in kinds else "feature"
    rec = {"slug": slug, "stage": stage,
           "changed_artifacts": sorted(p for s in arts.values() for _, _, p in s)}
    base_intent = gitio.exists(diff.repo, diff.base, f"docs/plans/{slug}/intent.md")
    base_spec = gitio.exists(diff.repo, diff.base, f"docs/plans/{slug}/spec.md")
    head_plan = diff.exists_head(f"docs/plans/{slug}/plan.md")
    rec.update(base_has_intent=base_intent, base_has_spec=base_spec, head_has_plan=head_plan)
    if stage == "spec" and not base_intent:
        problems.append("intent_not_on_base")
    if stage == "feature":
        if not base_spec:
            problems.append("spec_not_on_base")
        if not head_plan:
            problems.append("plan_not_in_head")
    new_artifact = [p for s in arts.values() for k, st, p in s if st == "A" and k in ("intent", "spec")]
    if new_artifact and any(p not in new_artifact for _, p, _ in statuses):
        problems.append("mixed_artifacts")
    problems = util.uniq(problems)
    if problems:
        rec["result"] = "chain_order"
        rec["problems"] = problems
        rec["next_action"] = "; ".join(NEXT[x].format(slug=slug) for x in problems)
    else:
        rec["result"] = "ok"
        rec["problems"] = []
    return rec


# ---------------------------------------------------------------- RV1
def run(diff, manifest, cfg, shared_globs, checks, criteria_tree, body_chain=None, history_rev=None,
        pending=None, rules_file="code_review.md"):
    statuses = diff.name_status()
    numstat = diff.numstat()
    excl_globs = exclusion_globs(manifest, cfg)

    def excluded(p):
        return is_lockfile(p) or util.first_match(excl_globs, p) is not None

    changed = [p for _, p, _ in statuses]
    excluded_files = sorted(p for p in changed if excluded(p))
    reviewed = [p for p in changed if not excluded(p)]
    added = sum(numstat.get(p, (0, 0))[0] for p in reviewed)
    deleted_n = sum(numstat.get(p, (0, 0))[1] for p in reviewed)
    hunks = diff.hunks()
    test_globs = test_globs_for(manifest)
    pend = sorted(set(pending if pending is not None else cfgmod.pending_verbs(manifest)))
    sig = signals(diff, statuses, hunks, manifest, shared_globs, test_globs, history_rev, pend, excluded_fn=excluded)
    sig["coverage"] = coverage(diff, hunks, manifest, cfg, excluded)
    fallback = (cfg.get("criteria") or {}).get("fallback_files") or ["CLAUDE.md", "AGENTS.md"]
    rv1 = {
        "checks": checks,
        "excluded": excluded_files,
        "diff_stats": {"files": len(changed), "added": added, "deleted": deleted_n},
        "criteria": criteria_map(changed, criteria_tree, rules_file, fallback) if changed else [],
        "signals": sig,
        "chain": chain_check(diff, statuses, body_chain),
    }
    rv1["_statuses"] = statuses
    rv1["_numstat"] = numstat
    rv1["_hunks"] = hunks
    rv1["_test_globs"] = test_globs
    rv1["_changed_lines"] = added + deleted_n
    rv1["_reviewed"] = reviewed
    return rv1


def public(rv):
    """Copy without the private `_` fields (what goes into review-report.json)."""
    return {k: v for k, v in rv.items() if not k.startswith("_")}
