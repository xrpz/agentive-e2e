# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV3], spec.spec_stage.chain.must_human.rv3, decisions.accepted[DEPHUMAN] (owner D)
"""RV3 change classification: rules first, then an LLM layer for what the rules leave undecided.

Per changed file, the first rule that decides wins (all [design judgment] except where the design names the rule):
  1 instruction and criteria files never count as docs (design: CLAUDE.md, AGENTS*.md, code_review.md, REVIEW.md;
    [design judgment] also the files other agents read as instructions or prompts: GEMINI.md, SKILL.md, *.agent.md,
    *.prompt.md, *.instructions.md, .github/copilot-instructions.md, .github/instructions/**, .github/prompts/**,
    .cursor/rules/**, .cursorrules, .windsurfrules, .clinerules): class config; chain artifacts
    docs/plans/<slug>/intent.md|spec.md: class other
  2 the project's classify.path_rules (a tier-0-eligible class from a path rule is kept only when the mechanical
    check of that class also holds; otherwise the file stays undecided)
  3 generated (agentive manifest paths.generated), lockfile (known lockfile names), package and build manifests before
    docs (requirements*.txt, constraints*.txt, Pipfile, setup.py (E2E-21) and the other package files: deps only when their
    dependency entries changed or could not be read (DEPHUMAN, rv1.dependency_changes package_files), else config, so a
    [tool.ruff] or package.json scripts edit is config; CMakeLists.txt: config),
    whitespace-only (format, when whitespace_only_is_format), docs (documentation files in documentation places)
  4 raises the rules can see: security (RV2 hit with reason auth, security, secrets or permissions), CI files and
    root config files (config), tests (test)
  5 anything else is undecided -> the LLM layer (task rv3-classifier) may pick only non-tier-0 classes; an invalid
    output, or llm_layer: false, gives class other (RV4 then decides on other, which is never tier 0)

Whitespace-only (V2-01, [design judgment]): `git diff -w` is not a mechanical proof, because whitespace carries meaning in
some languages and inside string literals (an indented `return` in Python changes the control flow; `return x` and
`returnx` are equal under -w). A modified file is format only when
  - Python (.py .pyi .pyw): both versions parse with the standard library `ast`, the ASTs are equal and the comments are
    equal (tokenize), so only layout changed;
  - a whitespace-significant file type (YAML, Makefile, Haskell, Nim, CoffeeScript, Pug, Haml, Slim, Sass, Stylus, F#,
    Elm, GDScript, shell scripts with heredocs, reStructuredText, Markdown, TSV, patches, snapshots): never;
  - any other text file: no version holds a multi-line string or heredoc opener (backtick, triple quotes, C++ raw
    strings, C# verbatim strings, Rust raw strings, heredocs), and the non-blank lines are equal one by one after
    collapsing each run of whitespace to one space and stripping both ends; a line that holds a quote character or
    ends in a backslash may differ only in its leading whitespace.
"""
import posixpath
import re

from . import config as cfgmod
from . import rv1 as rv1mod
from . import util

DOC_EXT = {".md", ".mdx", ".rst", ".adoc", ".asciidoc"}
TEXT_DOC_EXT = {".txt"}
DOC_ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
# design: CLAUDE.md, AGENTS*.md, code_review.md, REVIEW.md; [design judgment] the instruction and prompt files of other agents
NOT_DOCS = re.compile(r"(^|/)(CLAUDE(\.local)?\.md|AGENTS[^/]*\.md|code_review\.md|REVIEW\.md|GEMINI\.md|SKILL\.md"
                      r"|[^/]+\.(agent|prompt|instructions)\.md|copilot-instructions\.md|\.cursorrules|\.windsurfrules)$"
                      r"|^\.github/(instructions|prompts)/|(^|/)\.cursor/rules/|(^|/)\.clinerules(/|$)")
CHAIN_ART = re.compile(r"^docs/plans/(?!archive/)[^/]+/(intent|spec)\.md$")
# documentation places: the repo root, a directory named like docs, and the usual documentation file names anywhere
DOC_DIRS = {"docs", "doc", "documentation", "wiki", "adr", "adrs"}
DOC_NAMES = re.compile(r"^(README|CHANGELOG|CHANGES|HISTORY|NEWS|CONTRIBUTING|CODE_OF_CONDUCT|AUTHORS|CONTRIBUTORS|"
                       r"MAINTAINERS|LICEN[CS]E|COPYING|NOTICE|ARCHITECTURE|UPGRADING|MIGRATION|SUPPORT)([._-][^/]*)?$", re.I)
# package and build manifests that carry a documentation extension (V2-02)
DEPS_TEXT = re.compile(r"^(constraints|requirements)[\w.-]*\.(txt|in)$")
BUILD_TEXT = {"CMakeLists.txt"}
CI_GLOBS = [".github/workflows/**", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/**", "azure-pipelines.yml",
            ".buildkite/**", "bitbucket-pipelines.yml"]
CONFIG_EXT = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".xml"}
SECURITY_REASONS = {"auth", "security", "secrets", "permissions"}

PY_EXT = {".py", ".pyi", ".pyw"}
WS_SIGNIFICANT_EXT = {".yml", ".yaml", ".mk", ".hs", ".lhs", ".nim", ".nims", ".coffee", ".pug", ".jade", ".haml", ".slim",
                      ".sass", ".styl", ".fs", ".fsi", ".fsx", ".elm", ".gd", ".sh", ".bash", ".zsh", ".fish", ".ksh",
                      ".rst", ".md", ".mdx", ".tsv", ".diff", ".patch", ".snap"}
WS_SIGNIFICANT_NAMES = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Procfile"}
# multi-line string and heredoc openers: backtick, triple quotes, C++ R"x(, C# @", Rust r#", heredocs (<<EOF, <<-'EOF', <<~X, PHP <<<)
MULTILINE_OPENERS = re.compile(r"`|\"\"\"|'''|\bR\"[^(\s]*\(|@\"|\br#*\"|<<[-~]?\s*['\"]?[A-Za-z_]\w*|<<<")


def _is_doc(path):
    ext = posixpath.splitext(path)[1].lower()
    base = posixpath.basename(path)
    in_doc_dir = any(seg.lower() in DOC_DIRS for seg in path.split("/")[:-1])
    if ext in DOC_EXT:
        return "/" not in path or in_doc_dir or bool(DOC_NAMES.match(base))
    if ext in TEXT_DOC_EXT:
        return in_doc_dir or bool(DOC_NAMES.match(base))
    return in_doc_dir and ext in DOC_ASSET_EXT


def _manifest_class(path):
    """deps | config for package and build manifests, else None (checked before format and docs, V2-02). The class of a
    changed package file is decided in classify_rules from its dependency entries (DEPHUMAN)."""
    base = posixpath.basename(path)
    if rv1mod.is_package_file(path) or DEPS_TEXT.match(base):
        return "deps"
    if base in BUILD_TEXT:
        return "config"
    return None


def _is_root_config(path):
    base = posixpath.basename(path)
    if path.startswith(".config/") or path.startswith("config/"):
        return True
    if "/" in path:
        return False
    return base.startswith(".") or posixpath.splitext(base)[1].lower() in CONFIG_EXT


def _py_layout_only(old, new):
    """True when two Python sources have equal ASTs and equal comments (only the layout changed)."""
    import ast
    import io
    import tokenize

    def comments(src):
        return [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.COMMENT]
    try:
        return ast.dump(ast.parse(old)) == ast.dump(ast.parse(new)) and comments(old) == comments(new)
    except (SyntaxError, ValueError, tokenize.TokenError, RecursionError, MemoryError):
        return False


def _norm_lines(text):
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if "\"" in ln or "'" in ln or ln.rstrip().endswith("\\"):
            out.append(("q", ln.lstrip()))
        else:
            out.append(("t", " ".join(ln.split())))
    return out


def whitespace_only_texts(path, old, new):
    """The mechanical whitespace-only check of one modified file (V2-01, see the module docstring)."""
    if old is None or new is None or old == new:
        return False
    if "\0" in old or "\0" in new:
        return False
    base = posixpath.basename(path)
    ext = posixpath.splitext(base)[1].lower()
    if ext in PY_EXT:
        return _py_layout_only(old, new)
    if ext in WS_SIGNIFICANT_EXT or base in WS_SIGNIFICANT_NAMES or base.startswith("Dockerfile"):
        return False
    if MULTILINE_OPENERS.search(old) or MULTILINE_OPENERS.search(new):
        return False
    return _norm_lines(old) == _norm_lines(new)


def classify_rules(diff, statuses, manifest, rules, rv2_hits, dep_files=None):
    """({path: class}, [undecided paths]). dep_files: the package files whose dependency entries changed (RV1 signal
    dependency_changes.package_files); computed here when not given."""
    gen = [cfgmod.path_glob(p) for p in cfgmod.manifest_paths(manifest, "generated")]
    path_rules = (rules.get("classify") or {}).get("path_rules") or []
    ws_format = (rules.get("classify") or {}).get("whitespace_only_is_format", True)
    tests = rv1mod.test_globs_for(manifest)
    sec_paths = {h["path"] for h in rv2_hits if h["reason"] in SECURITY_REASONS}
    if dep_files is None:
        dep_files = rv1mod.dependency_changes(diff, statuses)["package_files"]
    dep_files = set(dep_files)
    decided, undecided = {}, []

    def whitespace_only(st, p):
        if st != "M" or not ws_format:
            return False
        if diff.patch([p], context=0, ignore_ws=True).strip() != "":     # cheap first filter: not even equal under -w
            return False
        return whitespace_only_texts(p, diff.show_base(p), diff.show_head(p))

    def mechanical(cls, st, p):
        return {"generated": util.first_match(gen, p) is not None, "lockfile": rv1mod.is_lockfile(p),
                "format": whitespace_only(st, p),
                "docs": _is_doc(p) and _manifest_class(p) is None and not NOT_DOCS.search(p) and not CHAIN_ART.match(p)}.get(cls, True)

    for st, p, _ in statuses:
        if NOT_DOCS.search(p):
            decided[p] = "config"
            continue
        if CHAIN_ART.match(p):
            decided[p] = "other"
            continue
        pr = next((e["class"] for e in path_rules if util.glob_match(e["glob"], p)), None)
        if pr is not None:
            if pr not in cfgmod.TIER0_ELIGIBLE or mechanical(pr, st, p):
                decided[p] = pr
            else:
                undecided.append(p)
            continue
        if p in sec_paths:
            decided[p] = "security"
        elif util.first_match(gen, p):
            decided[p] = "generated"
        elif rv1mod.is_lockfile(p):
            decided[p] = "lockfile"
        elif _manifest_class(p) == "deps":
            decided[p] = "deps" if p in dep_files else "config"        # DEPHUMAN: only a real dependency change is deps
        elif _manifest_class(p):
            decided[p] = _manifest_class(p)
        elif whitespace_only(st, p):
            decided[p] = "format"
        elif _is_doc(p):
            decided[p] = "docs"
        elif util.first_match(CI_GLOBS, p):
            decided[p] = "config"
        elif rv1mod.is_test(p, tests):
            decided[p] = "test"
        elif _is_root_config(p):
            decided[p] = "config"
        else:
            undecided.append(p)
    return decided, undecided


def validate_llm(out, undecided):
    """LLM layer output {"session_id", "classes": [..], "files": {path: class}?}: only non-tier-0 classes."""
    if not isinstance(out, dict):
        return None
    cls = out.get("classes")
    if not isinstance(cls, list) or not cls:
        return None
    for c in cls:
        if c not in cfgmod.CLASSES or c in cfgmod.TIER0_ELIGIBLE:
            return None
    return util.uniq(cls)


def finish(decided, undecided, llm_layer, llm_out=None):
    """review-report rv3 {classes, method, llm_valid?}."""
    classes = util.uniq(sorted(set(decided.values()), key=cfgmod.CLASSES.index))
    if not undecided:
        return {"classes": classes or ["other"], "method": "deterministic"}
    if not llm_layer:
        return {"classes": util.uniq(classes + ["other"]), "method": "deterministic"}
    got = validate_llm(llm_out, undecided) if llm_out is not None else None
    if got is None:
        return {"classes": util.uniq(classes + ["other"]), "method": "llm", "llm_valid": False}
    return {"classes": util.uniq(classes + got), "method": "llm", "llm_valid": True}
