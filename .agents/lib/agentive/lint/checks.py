# agentive-unit: lib.agentive
# design: spec.repo_checks.agentive_lint, spec.context_management.ci_size_checks, spec.spec_stage.provenance_tags, spec.plugin.invocation.repo_skills, spec.maintenance.supply_chain, spec.instructions.link_check (owner D)
"""The agentive-lint checks. Paths are the repository's tracked files (git ls-files), so build output and
.agents/state/ (gitignored) are never read.

Choices the design leaves open [design judgment]:
- size: SKILL.md tokens are approximated as ceil(bytes / 4); the Codex combined cap is checked for every AGENTS.md
  as the sum of the AGENTS.md files on the path from the repo root to its directory (project_doc_max_bytes 32 KiB);
- check-tags: a tag passes with a slug and a fix sentence of at least three words after the slug and an optional
  `(from <harvest-id|PR>)`; the distill markers `noise-<slug>` and `eval-<PR>` need no sentence (the design writes
  them without one); occurrences inside backticks are documentation, not tags; framework-generated files
  (agentive manifest `generated` keys) and .agents/lib/, .agents/bin/ are skipped;
- skills: a skill has side effects when SKILL.md or its scripts run git commit/push, gh api writes, gh pr/issue
  writes, a package publish or a release; shared skills (.agents/skills/) may not use Claude-only variables
  (${CLAUDE_...}) or the allowed-tools key;
- links: on when the ignore list `.agents/link-check.txt` exists (one glob per line; the design's one-time baseline
  cleanup creates it).
"""
import math
import os
import posixpath
import re
import subprocess

from .. import ayaml, common, rulesync

SIDE_EFFECT = re.compile(r"\bgit\s+(commit|push|tag)\b|\bgh\s+api\b[^\n]*-X\s*(POST|PATCH|PUT|DELETE)|\bgh\s+(pr|issue|release)\s+"
                         r"(create|merge|edit|close|comment)\b|\bnpm\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b")
FETCH_URL = re.compile(r"\b(curl|wget|WebFetch|fetch|download|Invoke-WebRequest)\b[^\n]*https?://", re.I)
SCRIPT_REF = re.compile(r"\b(?:bash|sh|python3|python|node)\s+((?:\.agents|\.claude)/skills/[\w.-]+/scripts/[\w./-]+|scripts/[\w./-]+)")
CLAUDE_VAR = re.compile(r"\$\{?CLAUDE_[A-Z_]+\}?")
TAG = re.compile(r"agentive-check:\s*(\S+)(.*)")
DECISION_TAG = re.compile(r"^(\[user\]|\[inferred\]|\[doc:\s*[^\]\s][^\]]*\])")
MODEL_INVOCABLE = {"reprove", "pr", "distill", "pr-feedback", "chain-intent", "chain-spec", "chain-plan",
                   "precommit-review", "verify", "janitor"}       # plugin.invocation.repo_skills (one list)
USER_ONLY = {"init", "update", "agentive-init", "agentive-update"}


class Problem:
    def __init__(self, sub, path, line, what, fix, warn=False):
        self.sub, self.path, self.line, self.what, self.fix, self.warn = sub, path, line, what, fix, warn

    def __str__(self):
        loc = self.path + (f":{self.line}" if self.line else "")
        return f"{'LINTWARN' if self.warn else 'LINT'} {self.sub} {loc}: {self.what}. NEXT: {self.fix}"


def tracked(repo):
    r = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True)
    files = [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p] if r.returncode == 0 else []
    return [p for p in files if os.path.isfile(os.path.join(repo, p))]


def read(repo, p):
    try:
        with open(os.path.join(repo, p), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def frontmatter(text):
    """(mapping or None, problem or None) of a leading --- YAML block."""
    if not text or not text.startswith("---"):
        return None, "no frontmatter"
    end = text.find("\n---", 3)
    if end == -1:
        return None, "frontmatter not closed"
    try:
        doc = ayaml.loads(text[3:end])
    except ayaml.YAMLError as e:
        return None, f"frontmatter: {e}"
    return (doc if isinstance(doc, dict) else None), (None if isinstance(doc, dict) else "frontmatter is not a mapping")


# ---------------------------------------------------------------- size
def size(repo, files):
    out = []
    agents = sorted(p for p in files if posixpath.basename(p) == "AGENTS.md")
    for p in agents:
        t = read(repo, p) or ""
        n = len(t.splitlines())
        if n > 200:
            out.append(Problem("size", p, None, f"{n} lines (limit 200)", "move details into skills, rules or docs and keep one pointer line"))
        chain = [q for q in agents if posixpath.dirname(q) == "" or posixpath.dirname(p) == posixpath.dirname(q)
                 or posixpath.dirname(p).startswith(posixpath.dirname(q) + "/")]
        total = sum(len((read(repo, q) or "").encode("utf-8")) for q in chain)
        if total > 32768:
            out.append(Problem("size", p, None, f"Codex combined AGENTS.md size {total} bytes on this path (limit 32 KiB)", "shorten the AGENTS.md files on this path"))
        elif total > 24576:    # NOMAP budget [design judgment]: warn before Codex's 32 KiB project_doc_max_bytes silently cuts the end
            out.append(Problem("size", p, None, f"Codex combined AGENTS.md size {total} bytes on this path (warning above 24 KiB, Codex reads 32 KiB)",
                               "shorten the AGENTS.md files on this path before the limit cuts them", warn=True))
    for p in files:
        if posixpath.basename(p) == "CLAUDE.md":
            n = len((read(repo, p) or "").splitlines())
            if n > 200:
                out.append(Problem("size", p, None, f"{n} lines (limit 200)", "move details out of CLAUDE.md"))
    for p in files:
        if posixpath.basename(p) == "SKILL.md" and (p.startswith(".agents/skills/") or p.startswith(".claude/skills/")):
            if p.startswith(".claude/skills/") and os.path.islink(os.path.join(repo, posixpath.dirname(p))):
                continue
            t = read(repo, p) or ""
            n, tok = len(t.splitlines()), math.ceil(len(t.encode("utf-8")) / 4)
            if n > 500:
                out.append(Problem("size", p, None, f"{n} lines (limit 500)", "move reference material into files the skill loads on demand"))
            if tok > 5000:
                out.append(Problem("size", p, None, f"about {tok} tokens (limit 5,000; bytes/4)", "move reference material into files the skill loads on demand"))
    return out


# ---------------------------------------------------------------- provenance
def provenance(repo, files):
    out = []
    for p in files:
        if not re.match(r"^docs/plans/.+/(plan|spec)\.md$", p):
            continue
        lines = (read(repo, p) or "").splitlines()
        in_sec, fence = False, False
        for i, ln in enumerate(lines, 1):
            if ln.startswith("```") or ln.startswith("~~~"):
                fence = not fence
                continue
            if fence:
                continue
            if ln.startswith("## "):
                in_sec = ln.strip() == "## Decisions"
                continue
            if not in_sec or not ln.strip() or ln.startswith("#") or ln.strip().startswith("<!--"):
                continue
            item = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", ln).strip()
            if not DECISION_TAG.match(item):
                what = "empty [doc:] tag" if item.startswith("[doc:") else "decision line without a provenance tag"
                out.append(Problem("provenance", p, i, what, "start the line with [user], [doc: <path|URL>] or [inferred]"))
    return out


# ---------------------------------------------------------------- check-tags
def check_tags(repo, files, generated=()):
    out = []
    skip = set(generated)
    for p in files:
        if p in skip or p.startswith((".agents/lib/", ".agents/bin/", ".agents/state/")) or p.endswith((".lock", "-lock.json", ".sum")):
            continue
        t = read(repo, p)
        if t is None or "agentive-check:" not in t:
            continue
        for i, ln in enumerate(t.splitlines(), 1):
            if "agentive-check:" not in ln:
                continue
            scrubbed = re.sub(r"`[^`]*`", "", ln)
            for m in TAG.finditer(scrubbed):
                slug, rest = m.group(1).rstrip(":,.;"), m.group(2)
                if not re.match(r"^[a-z0-9][a-z0-9.-]*$", slug):
                    out.append(Problem("check-tags", p, i, f"tag slug {slug!r} is not a lowercase slug", "write `agentive-check: <slug> <one fix sentence>`"))
                    continue
                rest = re.sub(r"-->.*$", "", rest)
                rest = re.sub(r"\(from [^)]*\)", "", rest).strip(" -:#*/")
                if slug.startswith(("noise-", "eval-")):
                    continue
                if len(re.findall(r"[A-Za-z]{2,}", rest)) < 3:
                    out.append(Problem("check-tags", p, i, f"tag {slug} has no fix sentence", "add one sentence that says how to fix what the check reports"))
    return out


# ---------------------------------------------------------------- persist
def persist(repo, files):
    out = []
    for p in files:
        if not re.match(r"^\.(agents|claude)/rules/.+\.md$", p):
            continue
        fm, _ = frontmatter(read(repo, p))
        if fm and fm.get("persist") is True and "paths" in fm:
            out.append(Problem("persist", p, None, "rule marked persist carries paths:", "remove paths: (a persist rule always loads) or drop persist"))
    return out


# ---------------------------------------------------------------- skills
def skills(repo, files):
    out = []
    dirs = sorted({posixpath.dirname(p) for p in files if posixpath.basename(p) == "SKILL.md"
                   and re.match(r"^\.(agents|claude)/skills/[^/]+/SKILL\.md$", p)})
    for d in dirs:
        if d.startswith(".claude/skills/") and os.path.islink(os.path.join(repo, d)):
            continue
        name = posixpath.basename(d)
        shared = d.startswith(".agents/skills/")
        sk = f"{d}/SKILL.md"
        text = read(repo, sk) or ""
        fm, prob = frontmatter(text)
        if fm is None:
            out.append(Problem("skills", sk, 1, prob, "start SKILL.md with --- name, description, disable-model-invocation ---"))
            continue
        if fm.get("name") != name:
            out.append(Problem("skills", sk, 1, f"frontmatter name {fm.get('name')!r} differs from the directory {name}", "set name to the directory name"))
        if not str(fm.get("description") or "").strip():
            out.append(Problem("skills", sk, 1, "no description", "write when to use the skill in description"))
        dmi = fm.get("disable-model-invocation")
        if "disable-model-invocation" not in fm:
            out.append(Problem("skills", sk, 1, "disable-model-invocation not written", "write disable-model-invocation: true or false explicitly"))
        scripts_text = ""
        for p in files:
            if p.startswith(d + "/scripts/"):
                scripts_text += (read(repo, p) or "") + "\n"
        if (name in USER_ONLY) and dmi is not True:
            out.append(Problem("skills", sk, 1, f"{name} must be user-invoked only", "set disable-model-invocation: true"))
        elif SIDE_EFFECT.search(text + "\n" + scripts_text) and dmi is not True and name not in MODEL_INVOCABLE:
            out.append(Problem("skills", sk, 1, "side-effect skill without disable-model-invocation: true",
                               "set disable-model-invocation: true, or record the skill in plugin.invocation.repo_skills (design change)"))
        for i, ln in enumerate(text.splitlines(), 1):
            if FETCH_URL.search(ln):
                out.append(Problem("skills", sk, i, "instruction to fetch an external URL", "vendor the content into the skill or the repo"))
            for ref in SCRIPT_REF.findall(ln):
                out += _script_ref(repo, d, sk, i, ref, shared)
            if shared and CLAUDE_VAR.search(ln):
                out.append(Problem("skills", sk, i, f"Claude-only variable {CLAUDE_VAR.search(ln).group(0)} in a skill shared with Codex",
                                   f"call scripts repo-root relative (sh {d}/scripts/<x>.sh) instead"))
        if shared and "allowed-tools" in fm:
            out.append(Problem("skills", sk, 1, "allowed-tools in a skill shared with Codex", "remove it; permissions live in settings and Codex rules"))
        oy = f"{d}/agents/openai.yaml"
        if shared and oy in files:
            try:
                doc = ayaml.loads(read(repo, oy) or "") or {}
            except ayaml.YAMLError as e:
                out.append(Problem("skills", oy, None, str(e), "fix the YAML"))
                continue
            pol = (doc.get("policy") or {}) if isinstance(doc, dict) else {}
            if "allow_implicit_invocation" not in pol and "allow_implicit_invocation" not in (doc if isinstance(doc, dict) else {}):
                out.append(Problem("skills", oy, None, "allow_implicit_invocation not written", "write policy.allow_implicit_invocation explicitly"))
    return out


def _script_ref(repo, d, sk, i, ref, shared):
    """SKILLPATHS (decisions.accepted, 2026-10-29): a shared skill calls its own scripts repo-root relative,
    `sh .agents/skills/<skill>/scripts/<x>.sh`, because both tools run Bash in the repository root and the headless
    allowlist `Bash(sh .agents/skills/*)` matches only that form; never through `..`. A Claude-only skill
    (.claude/skills/, the user's) may still use the skill-root form [design judgment]."""
    if ref.startswith("scripts/"):
        if shared:
            return [Problem("skills", sk, i, f"script {ref} is called skill-root relative",
                            f"call it from the repository root: sh {d}/{ref}")]
        return [] if os.path.isfile(os.path.join(repo, d, ref)) else [
            Problem("skills", sk, i, f"referenced script {ref} does not exist", f"add {d}/{ref} or fix the reference")]
    if ".." in ref.split("/"):
        return [Problem("skills", sk, i, f"script path {ref} contains ..", f"name the script directly: {d}/scripts/<x>")]
    if shared and not ref.startswith(d + "/scripts/"):
        return [Problem("skills", sk, i, f"script {ref} belongs to another skill", f"move the step into {d}/scripts/ (no shared utility skill) or call .agents/bin/<slug>")]
    if not os.path.isfile(os.path.join(repo, ref)):
        return [Problem("skills", sk, i, f"referenced script {ref} does not exist", f"add {ref} or fix the reference")]
    return []


# ---------------------------------------------------------------- rules
def rules(repo, files):
    """RULESNEUTRAL: AGENTS.md#path-rules and the pointer-mode .claude/rules copies match .agents/rules/*.md as the one
    renderer (agentive.rulesync, also used by /agentive:update) renders them; a merged rule has no agentive copy."""
    probs, _ = rulesync.sync(repo, write=False)
    return [Problem("rules", path, None, what, fix) for path, what, fix in probs]


# ---------------------------------------------------------------- lock
def lock(repo, files):
    p = ".agents/skills.lock"
    data = common.read_json(os.path.join(repo, p))
    if data is None:
        return [Problem("lock", p, None, "missing", "run /agentive:update to write .agents/skills.lock")]
    out = []
    for name, e in sorted((data.get("skills") or {}).items()):
        path = os.path.join(repo, e.get("path", ""))
        if not os.path.isdir(path):
            out.append(Problem("lock", p, None, f"skill {name}: {e.get('path')} does not exist", "restore the skill or run /agentive:update"))
            continue
        got = common.tree_sha256(path)
        if got != e.get("sha256"):
            out.append(Problem("lock", e.get("path", p), None, f"sha256 {got[:12]} differs from skills.lock {str(e.get('sha256'))[:12]}",
                               "revert the unreviewed skill change, or re-vendor it and update the lock in a reviewed PR"))
    return out


# ---------------------------------------------------------------- links
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
TICK_PATH = re.compile(r"`([\w.-]+(?:/[\w.-]+)+/?|[\w-]+\.(?:md|yaml|yml|json|sh|py|toml))`")


def links_enabled(repo):
    return os.path.isfile(os.path.join(repo, ".agents", "link-check.txt"))


def links(repo, files):
    from ..review import util
    ignore = [ln.strip() for ln in (read(repo, ".agents/link-check.txt") or "").splitlines() if ln.strip() and not ln.startswith("#")]
    out, tset = [], set(files)
    targets = [p for p in files if p == "AGENTS.md" or (p.startswith("docs/") and p.endswith(".md"))]
    for p in targets:
        for i, ln in enumerate((read(repo, p) or "").splitlines(), 1):
            refs = [(m, "link") for m in LINK.findall(ln) if not re.match(r"^[a-z]+:|^#", m)]
            refs += [(m, "path") for m in TICK_PATH.findall(ln)]
            for ref, kind in refs:
                rel = ref.split("#", 1)[0]
                if not rel or "<" in rel or "*" in rel:
                    continue
                # a Markdown link is relative to the file; a backtick path is repo-relative
                cand = posixpath.normpath(posixpath.join(posixpath.dirname(p), rel)) if kind == "link" else rel.rstrip("/")
                if util.first_match(ignore, cand) or util.first_match(ignore, rel):
                    continue
                if cand in tset or any(f.startswith(cand.rstrip("/") + "/") for f in tset) or os.path.exists(os.path.join(repo, cand)):
                    continue
                out.append(Problem("links", p, i, f"{ref} does not exist", "fix the path, or add it to .agents/link-check.txt (example or planned path)"))
    return out


def generated_paths(repo):
    m = read(repo, ".agents/manifest.yaml")
    if not m:
        return []
    try:
        doc = ayaml.loads(m) or {}
    except ayaml.YAMLError:
        return []
    return [k for k in (doc.get("generated") or {}) if "#" not in k]
