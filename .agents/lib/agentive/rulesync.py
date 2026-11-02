# agentive-unit: lib.agentive
# design: spec.repo_layer.files[f-rules], spec.init_apply.managed_regions, spec.repo_checks.agentive_lint, decisions.accepted[RULESNEUTRAL] · owner B
"""One renderer for the tool-neutral rules in .agents/rules/<name>.md (RULESNEUTRAL, 2026-10-29).

Used by the apply engine (/agentive:init, /agentive:update) and by `.agents/bin/agentive-lint rules --write|--check`,
so the AGENTS.md#path-rules region and the .claude/rules copies are the same whoever renders them.

- merged mode (the rules fit): every rule goes into AGENTS.md#path-rules (Codex reads AGENTS.md; Claude Code reads it
  through the CLAUDE.md `@AGENTS.md` import). Rules without `paths:` go under `### All files`, each path rule under
  `### <name> (paths: <globs>)`. No .claude/rules copy is written, so Claude never reads a rule twice [design judgment].
- pointer mode (AGENTS.md would pass 200 lines or 32 KiB): one pointer line per rule in the region and a
  .claude/rules/<name>.md copy (symlink or copy per T1) that Claude Code loads by its `paths:`.
Size test [design judgment]: lines and bytes of AGENTS.md OUTSIDE the managed regions, plus 40 lines for the context
region, plus the merged rules must stay <= 200 lines, and those bytes + 8 KiB <= 32 KiB (Codex project_doc_max_bytes).
Counting outside the regions keeps the choice the same before and after the region is written.
"""
import os
import re
import subprocess

from . import ayaml, common

__all__ = ["HEADING", "NONE_LINE", "collect", "plan", "region_body", "t1_mode", "sync"]

HEADING = "## Path rules (generated)"
NONE_LINE = "No rules yet."
REGION = "path-rules"
START = f"<!-- AGENTIVE START {REGION} -->"
END = f"<!-- AGENTIVE END {REGION} -->"
_MARK = re.compile(r"^\s*<!--\s*AGENTIVE (START|END) (\S+)\s*-->\s*$")
_PROV = re.compile(r"^\s*(<!--\s*)?#?\s*(Verified \d{4}-\d{2}-\d{2} against \S+\. )?Managed by agentive")   # the constant notice line (REGIONNOSHA) or the older dated one
CONTEXT_REGION_LINES = 40
LINE_LIMIT, BYTE_LIMIT, BYTE_SLACK = 200, 32768, 8192


def split_frontmatter(text):
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S) if text.startswith("---") else None
    if not m:
        return {}, text
    try:
        fm = ayaml.loads(m.group(1)) or {}
    except Exception:  # noqa: BLE001  (a broken frontmatter reads as no frontmatter, like the engine)
        fm = {}
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def _paths(fm):
    p = fm.get("paths") or []
    return [str(x) for x in (p if isinstance(p, list) else [p])]


def collect(repo):
    """[(name, text)] of the regular files .agents/rules/*.md on disk, sorted by name."""
    d = os.path.join(repo, ".agents", "rules")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if n.endswith(".md") and os.path.isfile(p) and not os.path.islink(p):
                with open(p, encoding="utf-8", newline="") as f:
                    out.append((n[:-3], f.read()))
    return out


def outside_regions(agents_text):
    """(lines, bytes) of AGENTS.md without the agentive managed regions."""
    keep, inside = [], False
    for ln in (agents_text or "").splitlines():
        m = _MARK.match(ln)
        if m:
            inside = m.group(1) == "START"
            continue
        if not inside:
            keep.append(ln)
    return len(keep), len("\n".join(keep).encode("utf-8"))


def _demote(body, by):
    """Every Markdown heading of a rule body moves `by` levels down, so it sits below the rule's own heading."""
    return re.sub(r"(?m)^(#+)", lambda m: m.group(1) + "#" * by, body.strip())


def plan(rules, agents_text=""):
    """{mode: none|merged|pointer, block: region text after the heading, mirrors: [names that get a .claude/rules copy]}."""
    rules = sorted(rules, key=lambda r: r[0])
    if not rules:
        return {"mode": "none", "block": NONE_LINE, "mirrors": []}
    parsed = [(name,) + split_frontmatter(text) for name, text in rules]
    glob_ = [(n, b) for n, fm, b in parsed if not _paths(fm)]
    scoped = [(n, _paths(fm), b) for n, fm, b in parsed if _paths(fm)]
    chunks = []
    if glob_:
        chunks.append("### All files\n\n" + "\n\n".join(f"#### {n}\n{_demote(b, 4)}" for n, b in glob_))
    chunks += [f"### {n} (paths: {', '.join(p)})\n{_demote(b, 3)}" for n, p, b in scoped]
    merged = "\n\n".join(chunks)
    lines, size = outside_regions(agents_text)
    if lines + CONTEXT_REGION_LINES + merged.count("\n") + 3 <= LINE_LIMIT and size + len(merged.encode("utf-8")) + BYTE_SLACK <= BYTE_LIMIT:
        return {"mode": "merged", "block": merged, "mirrors": []}
    out = []
    if glob_:
        out += ["### All files"] + [f"- {n}: read .agents/rules/{n}.md before any change" for n, _ in glob_]
    if scoped:
        out += ["### By path"] + [f"- {n}: read .agents/rules/{n}.md before you change a file matching {', '.join(p)}" for n, p, _ in scoped]
    return {"mode": "pointer", "block": "\n".join(out), "mirrors": [n for n, _ in rules]}


def region_body(block):
    return f"{HEADING}\n\n{block}\n"


def _norm(text):
    out = [ln.rstrip() for ln in (text or "").splitlines()]
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out)


def t1_mode(repo):
    """The symlink-or-copy rule (T1) the engine applies without a probe: copy on Windows or with core.symlinks=false."""
    if os.name == "nt":
        return "copy"
    r = subprocess.run(["git", "-C", repo, "config", "--get", "core.symlinks"], capture_output=True, text=True)
    return "copy" if r.stdout.strip().lower() == "false" else "symlink"


def _region_span(lines):
    s = [i for i, ln in enumerate(lines) if ln.strip() == START]
    e = [i for i, ln in enumerate(lines) if ln.strip() == END]
    if len(s) != 1 or len(e) != 1 or s[0] > e[0]:
        return None
    return s[0], e[0]


def _manifest_mirrors(repo):
    try:
        doc = ayaml.load(os.path.join(repo, ".agents", "manifest.yaml")) or {}
    except Exception:  # noqa: BLE001
        return {}
    gen = doc.get("generated") or {}
    return {k: v for k, v in gen.items() if re.match(r"^\.claude/rules/[^/]+\.md$", k) and isinstance(v, dict)}


def _state_sha(path):
    if os.path.islink(path):
        return common.sha256_text("symlink:" + os.readlink(path))
    if os.path.isfile(path):
        return common.sha256_file(path)
    return None


def sync(repo, write=False):
    """Compare (and with write=True fix) AGENTS.md#path-rules and the .claude/rules copies against .agents/rules.
    Returns (problems, changed): problems are (path, what, next) tuples of what differs (before the fix), changed the
    paths written or removed."""
    rules = collect(repo)
    texts = dict(rules)
    agents_p = os.path.join(repo, "AGENTS.md")
    agents = None
    if os.path.isfile(agents_p):
        with open(agents_p, encoding="utf-8", newline="") as f:
            agents = f.read()
    pl = plan(rules, agents or "")
    problems, changed = [], []
    lines = (agents or "").splitlines(keepends=True)
    span = _region_span(lines) if agents is not None else None
    if span is None:
        if rules:
            problems.append(("AGENTS.md", "no single AGENTS.md#path-rules region for the rules in .agents/rules/",
                             "run /agentive:update (Codex: $agentive-update) to write the region"))
    else:
        inner = lines[span[0] + 1:span[1]]
        prov = [inner[0]] if inner and _PROV.match(inner[0]) else []
        cur = "".join(inner[len(prov):])
        want = region_body(pl["block"])
        if _norm(cur) != _norm(want):
            problems.append(("AGENTS.md", f"region path-rules differs from what .agents/rules renders ({pl['mode']} mode)",
                             "run .agents/bin/agentive-lint rules --write and commit AGENTS.md with the rule"))
            if write:
                nl = "\r\n" if lines[span[0]].endswith("\r\n") else "\n"
                body = "".join(ln + nl for ln in _norm(want).split("\n"))
                text = "".join(lines[:span[0] + 1]) + "".join(prov) + body + "".join(lines[span[1]:])
                with open(agents_p, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                changed.append("AGENTS.md")
    mode = t1_mode(repo)
    want_mirrors = set(pl["mirrors"])
    for n in sorted(want_mirrors):
        rel = f".claude/rules/{n}.md"
        p = os.path.join(repo, rel)
        target = f"../../.agents/rules/{n}.md"
        ok = (os.path.islink(p) and os.readlink(p) == target) if mode == "symlink" else (
            os.path.isfile(p) and not os.path.islink(p) and open(p, encoding="utf-8", newline="").read() == texts[n])
        if ok:
            continue
        problems.append((rel, f"the pointer-mode copy of .agents/rules/{n}.md is missing or differs",
                         "run .agents/bin/agentive-lint rules --write and commit the copy with the rule"))
        if write:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.islink(p) or os.path.isfile(p):
                os.unlink(p)
            if mode == "symlink":
                os.symlink(target, p)
            else:
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(texts[n])
            changed.append(rel)
    recorded = _manifest_mirrors(repo)
    candidates = {f".claude/rules/{n}.md" for n in texts} | set(recorded)
    for rel in sorted(candidates):
        n = rel[len(".claude/rules/"):-3]
        p = os.path.join(repo, rel)
        if n in want_mirrors or not os.path.lexists(p):
            continue
        ours = (os.path.islink(p) and os.readlink(p) == f"../../.agents/rules/{n}.md") \
            or (n in texts and os.path.isfile(p) and open(p, encoding="utf-8", newline="").read() == texts[n]) \
            or (rel in recorded and _state_sha(p) == recorded[rel].get("sha256"))
        if not ours:
            continue      # the user's own Claude-only rule file: never touched
        why = "merged into AGENTS.md" if n in texts else "its source .agents/rules file is gone"
        problems.append((rel, f"agentive copy of a rule that is {why} (Claude would read it twice or read a deleted rule)",
                         "run .agents/bin/agentive-lint rules --write to remove it"))
        if write:
            os.unlink(p)
            changed.append(rel)
    return problems, changed
