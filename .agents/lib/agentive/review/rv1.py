# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV1], spec.review_pipeline.criteria_scope, spec.spec_stage.chain.ci_check, spec.one_way.module, decisions.accepted[LOCKFILEDEPS], decisions.accepted[DEPHUMAN], decisions.accepted[LOCALTIER] (owner D)
"""RV1 deterministic preprocess (llm: false).

Input: a gitio.Diff, the agentive manifest, the effective review config, the rubric shared_globs, the check
results, the criteria tree (paths of the tree whose criteria files apply) and the PR body chain block.
Output: the review-report `rv1` object plus private fields (prefixed `_`) the later stages use.

Detection choices the design leaves open, all [design judgment]:
- tests: agentive manifest paths.tests, else common test-file name patterns (stack-agnostic list below);
- public API: removed lines in paths.public_api files that look like an exported declaration;
- dependency changes (DEPHUMAN, E2E-20): only the dependency entries of package files (package.json, requirements*.txt,
  constraints*.txt, pyproject.toml, go.mod, Cargo.toml, pom.xml, build.gradle[.kts], Pipfile, setup.py (E2E-21)) compared
  between base and head (added, removed, changed spec), plus every lockfile whose content changed; the tool configuration
  that shares a package file ([tool.ruff], package.json scripts, ...) is no dependency; an unreadable package file counts as
  changed (fail closed); new direct dependencies = the added dependency names (go indirect requires left out);
- env vars: keys of .env.example-type files plus names read through common environment lookups in added or removed
  lines that do not occur in the other tree; files under .agents/, .claude/ and .codex/ are not read (E2E-35);
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
AGENTIVE_DIRS = (".agents", ".claude", ".codex")     # E2E-35: agentive-owned paths, not read for the env-var signal
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
# DEPHUMAN (E2E-20, user 2026-10-31): a parser returns {key: spec} for the dependency entries of one package file only (never
# the tool configuration that shares the file, such as [tool.ruff] or package.json scripts), or None when the file cannot be
# read (fail closed: an unreadable package file counts as a dependency change). key = "<ecosystem>:<name>" for a dependency
# (new_direct_dependencies lists these) and "<ecosystem>-<kind>:<name>" for an entry that changes where or which version is
# resolved (npm overrides, pip options such as --index-url, go replace, cargo patch, maven parent, build plugins and
# properties); spec = the group it sits in plus its version text, whitespace dropped, so a moved or re-pinned entry is
# "changed" [design judgment: the kinds and the small TOML reader; no tomllib yet; the floor python 3.13 has it, switching is a separate change].
DIRECT_ECOSYSTEMS = ("npm", "pypi", "go", "cargo", "maven")


def _collect(pairs):
    out = {}
    for k, v in pairs:
        out.setdefault(k, []).append(v)
    return {k: " | ".join(sorted(set(v))) for k, v in out.items()}


def _pypi(name):
    return "pypi:" + re.sub(r"[-_.]+", "-", name).lower()


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def _spec(table, attr, val):
    """'<table>:<value>' or, for the table form ([dependencies.<name>] version = ...), '<table>:<attr>=<value>'."""
    return ".".join(table) + ":" + (".".join(attr) + "=" if attr else "") + _squash(val)


def _toml_scan(val):
    """(bracket depth, [(string, brace depth)]) of a TOML value text: strings outside comments, with the inline-table depth
    they sit in."""
    depth, brace, strings, i, n = 0, 0, [], 0, len(val)
    while i < n:
        c = val[i]
        if c == "#":
            j = val.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "\"'":
            q = val[i:i + 3] if val[i:i + 3] in ('"""', "'''") else c
            j, buf = i + len(q), []
            while j < n and val[j:j + len(q)] != q:
                if q == '"' and val[j] == "\\" and j + 1 < n:
                    buf.append(val[j + 1])
                    j += 2
                    continue
                buf.append(val[j])
                j += 1
            strings.append(("".join(buf), brace))
            i = j + len(q)
            continue
        if c in "[{":
            depth += 1
            brace += c == "{"
        elif c in "]}":
            depth -= 1
            brace -= c == "}"
        i += 1
    return depth, strings


KEY_PART = re.compile(r"\s*(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|([A-Za-z0-9_-]+))\s*")


def _toml_key(text):
    parts, pos = [], 0
    while pos < len(text):
        m = KEY_PART.match(text, pos)
        if not m:
            return None
        parts.append(next(g for g in m.groups() if g is not None))
        pos = m.end()
        if pos < len(text):
            if text[pos] != ".":
                return None
            pos += 1
    return tuple(parts) if parts else None


def _toml_entries(text):
    """[(key path, value text)] of a TOML text: the table header joined with the (dotted) key; an array or inline table value
    spans lines until its brackets close outside strings and comments."""
    lines = (text or "").splitlines()
    out, sec, i = [], (), 0
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^\[\[?(.+?)\]\]?\s*(?:#.*)?$", s)
        if m:
            sec = _toml_key(m.group(1)) or ()
            continue
        m = re.match(r"^((?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[A-Za-z0-9_.\-\s])+?)\s*=\s*(.*)$", s)
        if not m:
            continue
        key = _toml_key(m.group(1).strip())
        if key is None:
            continue
        val = m.group(2)
        while _toml_scan(val)[0] > 0 and i < len(lines):
            val += "\n" + lines[i]
            i += 1
        out.append((sec + key, val))
    return out


def _requirement(s):
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", s)
    return (_pypi(m.group(1)), _squash(s)) if m else None


def _deps_package_json(text):
    try:
        d = json.loads(text) if (text or "").strip() else {}
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    pairs = []
    for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(d.get(k), dict):
            pairs += [(f"npm:{n}", f"{k}:{_squash(json.dumps(v, sort_keys=True))}") for n, v in d[k].items()]
    if isinstance(d.get("bundleDependencies") or d.get("bundledDependencies"), list):
        pairs += [(f"npm:{n}", "bundleDependencies") for n in (d.get("bundleDependencies") or d.get("bundledDependencies")) if isinstance(n, str)]
    overrides = [("overrides", d.get("overrides")), ("resolutions", d.get("resolutions")),
                 ("pnpm.overrides", (d.get("pnpm") or {}).get("overrides") if isinstance(d.get("pnpm"), dict) else None)]
    for group, v in overrides:
        if isinstance(v, dict):
            pairs += [(f"npm-override:{n}", f"{group}:{_squash(json.dumps(x, sort_keys=True))}") for n, x in v.items()]
    return _collect(pairs)


def _deps_requirements(text):
    """requirements*.txt, requirements*.in and constraints*.txt: one requirement per line; an option line (-r, -c, -e,
    --index-url, ...) is an entry of its own (it changes what is installed or from where)."""
    pairs = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"(^|\s)#.*$", "", ln).strip()
        if not ln:
            continue
        if ln.startswith("-"):
            pairs.append(("pypi-option:" + " ".join(ln.split()), ""))
            continue
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", ln) or ln.startswith((".", "/")):
            pairs.append(("pypi-url:" + _squash(ln), ""))          # a direct URL or local path requirement
            continue
        pairs.append(_requirement(ln) or ("pypi-other:" + _squash(ln), ""))
    return _collect(pairs)


PY_ARRAYS = {("project", "dependencies"), ("build-system", "requires"), ("tool", "uv", "dev-dependencies"),
             ("tool", "uv", "constraint-dependencies"), ("tool", "uv", "override-dependencies")}
PY_ARRAY_TABLES = {("project", "optional-dependencies"), ("dependency-groups",), ("tool", "pdm", "dev-dependencies")}


def _poetry_table(path):
    """(table length, name) when path sits in a poetry dependency table: tool.poetry.[group.<g>.](dev-)dependencies.<name>."""
    for n in (3, 5):
        t = path[:n]
        if len(path) > n and t[:2] == ("tool", "poetry") and t[-1] in ("dependencies", "dev-dependencies") and (n == 3 or t[2] == "group"):
            return n, path[n]
    return None


def _deps_pyproject(text):
    """Only the dependency keys of pyproject.toml (F5 of the E2E-20 research: the old reader took every list key, so
    `[tool.ruff] extend-select = ["B"]` became the dependency pypi:b): [project] dependencies, [project.optional-dependencies],
    [dependency-groups], [build-system] requires, [tool.uv] dev-, constraint- and override-dependencies,
    [tool.pdm.dev-dependencies] and the poetry dependency tables; never [tool.ruff], [tool.pytest], scripts or metadata."""
    pairs = []
    for path, val in _toml_entries(text):
        group = ".".join(path)
        if path in PY_ARRAYS or path[:-1] in PY_ARRAY_TABLES or path in PY_ARRAY_TABLES:
            inline_table = path in PY_ARRAY_TABLES
            for s, brace in _toml_scan(val)[1]:
                if brace and not inline_table:
                    continue                  # {include-group = "x"} in [dependency-groups] names a group, not a package
                r = _requirement(s)
                if r:
                    pairs.append((r[0], f"{group}:{r[1]}"))
            continue
        pt = _poetry_table(path)
        if pt and pt[1].lower() != "python":
            n, name = pt
            pairs.append((_pypi(name), _spec(path[:n], path[n + 1:], val)))
    return _collect(pairs)


def _deps_pipfile(text):
    """E2E-21: the package categories of a Pipfile ([packages], [dev-packages] and custom categories: every table except
    source, requires, scripts and pipenv)."""
    pairs = []
    for path, val in _toml_entries(text):
        if len(path) >= 2 and path[0] not in ("source", "requires", "scripts", "pipenv"):
            pairs.append((_pypi(path[1]), _spec(path[:1], path[2:], val)))
    return _collect(pairs)


def _deps_setup_py(text):
    """E2E-21: the quoted requirements in install_requires / extras_require / tests_require / setup_requires of a setup.py
    [design judgment: no evaluation of the Python file; a list built in code is not seen, the file still counts as a
    dependency file]."""
    pairs = []
    for key, block in re.findall(r"(?s)(install_requires|extras_require|tests_require|setup_requires)\s*=\s*([\[{].*?[\]}])", text or ""):
        block = re.sub(r"['\"][^'\"]*['\"]\s*:", "", block)          # extras_require keys are extra names, not packages
        for s in re.findall(r"['\"]([A-Za-z0-9][^'\"]*)['\"]", block):
            r = _requirement(s)
            if r:
                pairs.append((r[0], f"{key}:{r[1]}"))
    return _collect(pairs)


def _deps_go_mod(text):
    pairs, block = [], None
    for ln in (text or "").splitlines():
        s = ln.split("//", 1)[0].strip()
        indirect = "// indirect" in ln
        if re.match(r"^(require|replace)\s*\($", s):
            block = s.split()[0].rstrip("(")
            continue
        if block and s == ")":
            block = None
            continue
        m = re.match(r"^(require|replace)\s+(.*)$", s)
        kind, rest = (m.group(1), m.group(2)) if m else (block, s)
        if kind == "require":
            m = re.match(r"^([\w.\-/~]+\.[\w.\-/~]+)\s+(v\S+)", rest)
            if m:
                pairs.append(("go:" + m.group(1), m.group(2) + (" indirect" if indirect else "")))
        elif kind == "replace" and "=>" in rest:
            left, right = rest.split("=>", 1)
            pairs.append(("go-replace:" + " ".join(left.split()), " ".join(right.split())))
    return _collect(pairs)


CARGO_GROUPS = ("dependencies", "dev-dependencies", "build-dependencies")


def _deps_cargo(text):
    """[dependencies], [dev-dependencies], [build-dependencies], their target.<cfg>. and workspace. forms, the table form
    [dependencies.<name>], and [patch.<registry>] entries (cargo-patch)."""
    pairs = []
    for path, val in _toml_entries(text):
        if path[:1] == ("patch",) and len(path) >= 3:
            pairs.append(("cargo-patch:" + path[2], _spec(path[:2], path[3:], val)))
            continue
        for i, part in enumerate(path):
            if part in CARGO_GROUPS and (i == 0 or path[0] in ("target", "workspace")) and len(path) > i + 1:
                pairs.append(("cargo:" + path[i + 1], _spec(path[:i + 1], path[i + 2:], val)))
                break
    return _collect(pairs)


def _deps_pom(text):
    """<dependency> blocks (dependencies and dependencyManagement), <parent>, build <plugin> blocks and the <properties>
    block (versions are often properties) [design judgment]."""
    text = re.sub(r"(?s)<!--.*?-->", "", text or "")
    pairs = []

    def coords(blk):
        g = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", blk)
        a = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", blk)
        return ((g.group(1) + ":") if g else "") + a.group(1) if a else None
    for tag, kind in (("dependency", "maven"), ("plugin", "maven-plugin"), ("parent", "maven-parent")):
        for blk in re.findall(rf"(?s)<{tag}>(.*?)</{tag}>", text):
            c = coords(blk)
            if c:
                pairs.append((f"{kind}:{c}", _squash(blk)))
    for blk in re.findall(r"(?s)<properties>(.*?)</properties>", text):
        pairs.append(("maven-properties:pom", _squash(blk)))
    return _collect(pairs)


GRADLE_CONF = (r"implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|testCompileOnly|"
               r"annotationProcessor|kapt|ksp|classpath|developmentOnly|compile|testCompile|runtime")


def _deps_gradle(text):
    pairs = []
    for m in re.finditer(rf"(?m)^\s*({GRADLE_CONF})\s*\(?\s*(?:platform\s*\(\s*)?['\"]([^:'\"]+):([^:'\"]+)(?::([^'\"]*))?['\"]", text or ""):
        pairs.append((f"maven:{m.group(2)}:{m.group(3)}", f"{m.group(1)}:{m.group(4) or ''}"))
    for m in re.finditer(r"(?m)^\s*id\s*\(?\s*['\"]([^'\"]+)['\"]\s*\)?\s*(?:version\s*\(?\s*['\"]([^'\"]+)['\"])?", text or ""):
        pairs.append((f"gradle-plugin:{m.group(1)}", m.group(2) or ""))
    return _collect(pairs)


PACKAGE_FILES = [("package.json", _deps_package_json), ("pyproject.toml", _deps_pyproject),
                 ("go.mod", _deps_go_mod), ("Cargo.toml", _deps_cargo), ("pom.xml", _deps_pom),
                 ("build.gradle", _deps_gradle), ("build.gradle.kts", _deps_gradle),
                 ("Pipfile", _deps_pipfile), ("setup.py", _deps_setup_py)]     # E2E-21: Pipfile and setup.py are dependency files


def package_parser(path):
    b = posixpath.basename(path)
    for name, fn in PACKAGE_FILES:
        if b == name:
            return fn
    if re.match(r"^(requirements|constraints)[\w.-]*\.(txt|in)$", b):
        return _deps_requirements
    return None


def is_package_file(path):
    return package_parser(path) is not None


def dependency_changes(diff, statuses):
    """DEPHUMAN: what changed in the dependency data of this diff.
    {added, removed, changed: [{name, from, to}], lockfiles, unreadable, package_files}: added / removed / changed are the
    dependency entries of package files compared between base and head; lockfiles = lockfiles whose content changed (added,
    deleted, modified; a pure rename is no change); unreadable = package files that changed but could not be read (fail
    closed); package_files = package files whose dependency entries changed or that could not be read (RV3 class deps; a
    package file whose dependency entries are equal, such as a [tool.ruff] or scripts edit, is class config)."""
    out = {"added": [], "removed": [], "changed": [], "lockfiles": [], "unreadable": [], "package_files": []}
    direct = []
    for st, p, old in statuses:
        head = None if st == "D" else diff.show_head(p)
        base = None if st == "A" else diff.show_base(old or p)
        if is_lockfile(p) or (old and is_lockfile(old)):
            if st != "R" or (head or "") != (base or ""):
                out["lockfiles"].append(p)
            continue
        fn = package_parser(p) or (package_parser(old) if old else None)
        if not fn or (head or "") == (base or ""):
            continue
        h, b = fn(head or ""), fn(base or "")
        if h is None or b is None:
            out["unreadable"].append(p)
            out["package_files"].append(p)
            continue
        added, removed = sorted(set(h) - set(b)), sorted(set(b) - set(h))
        direct += [k for k in added if k.split(":", 1)[0] in DIRECT_ECOSYSTEMS and not h[k].endswith(" indirect")]
        changed = [{"name": k, "from": b[k], "to": h[k]} for k in sorted(set(h) & set(b)) if h[k] != b[k]]
        if added or removed or changed:
            out["package_files"].append(p)
        out["added"] += added
        out["removed"] += removed
        out["changed"] += changed
    for k in ("added", "removed", "lockfiles", "unreadable", "package_files"):
        out[k] = util.uniq(out[k])
    out["_direct_added"] = sorted(set(direct))           # private: RV1 new_direct_dependencies (go indirect requires left out)
    return out


def is_dependency_change(dc):
    return bool(dc and (dc.get("added") or dc.get("removed") or dc.get("changed") or dc.get("lockfiles") or dc.get("unreadable")))


def _few(items, n=5):
    items = list(items)
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


DEP_REVIEW_TEXT = {         # E2E-31: plain words for the state the check list shows; the state never decides the tier (DEPHUMAN)
    "pass": "dependency review ran and found no critical CVE",
    "fail": "dependency review found a critical CVE (see the dependency-review job)",
    "skipped": "dependency review could not run (usually the dependency graph is off, or a private repository has no GitHub Code "
               "Security)",
    None: "dependency review did not run for this commit",
}
# "skipped" = the dependency-review job ran but its action did not complete (the job's step prints status=unavailable, the checks
# helper records it as skipped); None = no entry: the job did not run in this event. A reported critical CVE is not information
# only: it keeps its own always_human line and failing check.
DEPENDENCY_NEXT = ("a human reviews the dependency change: why each package is needed, that each name is the official package "
                   "(no typosquatting), when each new version was published, and the dependency-review result; then merges")


def dependency_change_text(dc, dep_review="no_ci"):
    """The one-line RV2 always_human detail of DEPHUMAN: what changed, the rule, and (in CI) the dependency-review state as
    information. dep_review: pass | fail | skipped (the action did not complete) | None (the job did not run) | "no_ci"
    (precommit, reviewer eval: no line).
    E2E-31 (decision e2e_28_31): one short sentence; the summary comment shows it once (post.tier_reasons)."""
    what = []
    if dc.get("added"):
        what.append("added " + _few(dc["added"]))
    if dc.get("changed"):
        what.append("changed " + _few(util.uniq(c["name"] for c in dc["changed"])))
    if dc.get("removed"):
        what.append("removed " + _few(dc["removed"]))
    if dc.get("lockfiles"):
        what.append("lockfile " + _few(dc["lockfiles"]))
    if dc.get("unreadable"):
        what.append("could not read " + _few(dc["unreadable"]) + ", counted as changed")
    text = "human review needed: dependency change (" + "; ".join(what) + "), never auto-merged"
    if dep_review != "no_ci":
        text += "; " + DEP_REVIEW_TEXT.get(dep_review, DEP_REVIEW_TEXT[None]) + ("" if dep_review == "fail" else " (information only)")
    return text + "."


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
    # dependency changes (DEPHUMAN) and the new direct dependencies among them (go indirect requires are not direct)
    dc = dependency_changes(diff, statuses)
    s["new_direct_dependencies"] = dc.pop("_direct_added")
    s["dependency_changes"] = dc
    # env vars added or removed. E2E-35 [design judgment]: files under the agentive-owned directories (.agents/, .claude/,
    # .codex/) are left out: they are RV2 protected check_config (tier 2) already, so their env lookups only add noise
    env = set()
    for st, p, old in statuses:
        if any(x.split("/", 1)[0] in AGENTIVE_DIRS for x in (p, old) if x):
            continue
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
