# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV2].security_paths, decisions.accepted[SECPATHS] (owner D)
"""Security-sensitive path detection helpers shared by RV2, the local PR-body estimate and the init/update engine
(stdlib only). Decision SECPATHS (2026-10-29): four layers, case-sensitive matching.

Matching (all case-sensitive, like CODEOWNERS; there is no case-insensitive switch):
  glob     `util.glob_match`, extended so that a glob also matches a directory and everything below it
           (`**/*secret*` matches `secrets/prod.yaml`, `**/permissions*` matches `src/permissions/x.py`) [design judgment]
  token    a path segment is split at `_`, `-`, `.` and spaces; the entry matches when one token of one segment is equal to
           the entry's token (`auth` matches `auth/`, `auth_service.py`, `login-auth.ts`, never `author.py`) [design judgment]
  content  `{path, contains}`: the glob matches the changed file and an added or removed line contains the text
           (plain substring, no regular expression), the Gerrit `withDiffContaining` idea [Gerrit submit requirements]

An entry of `never_ai_approve` (and of `add`) is one of
  {glob, reason}  {token, reason}  {tokens: [..], reason}  and, only in `content_rules` and `add`,
  {path, contains, reason}   (`contains` is a string or a list of strings; one of them is enough)
plus the optional `source` (`codeowners:<line>`, `preset:<id>`), `note` and `shadow: true` (report only, no tier effect).
"""
import re

from . import util

TOKEN_SPLIT = re.compile(r"[_\-. ]+")
OWNER_SPLIT = re.compile(r"[^A-Za-z0-9]+")
# owners whose name looks security related; matched on tokens, never on substrings (`william` is not `iam`) [design judgment]
OWNER_WORDS = ("security", "appsec", "secops", "sre", "infosec", "iam", "privacy")
OWNER_PREFIX_WORDS = ("security", "appsec", "secops", "infosec")      # `securityteam` counts, `sre` and `iam` must be a whole token


def tokens_of(segment):
    return [t for t in TOKEN_SPLIT.split(segment) if t]


def token_match(token, path):
    return any(token in tokens_of(seg) for seg in path.split("/"))


def glob_dir_match(glob, path):
    """util.glob_match, plus: a glob that does not end in `**` also covers everything below a directory it matches."""
    if util.glob_match(glob, path):
        return True
    if glob.endswith("**"):
        return False
    return util.glob_match(glob + "/**", path)


def first_dir_match(globs, path):
    for g in globs or []:
        if glob_dir_match(g, path):
            return g
    return None


# ---------------------------------------------------------------- entries
def expand(entries):
    """`{tokens: [a, b], reason}` -> one `{token: a, reason}` per token; every other entry is copied as is."""
    out = []
    for e in entries or []:
        if isinstance(e, dict) and isinstance(e.get("tokens"), list):
            for t in e["tokens"]:
                d = {k: v for k, v in e.items() if k != "tokens"}
                d["token"] = t
                out.append(d)
        else:
            out.append(dict(e))
    return out


def contains_list(e):
    c = e.get("contains")
    return [c] if isinstance(c, str) else list(c or [])


def key(e):
    """Identity of an entry for remove, dedupe and the update diff."""
    if "token" in e:
        return ("token", e["token"])
    if "contains" in e:
        return ("content", e.get("path"), tuple(contains_list(e)))
    return ("glob", e.get("glob"))


def label(e):
    """The `glob` string recorded in a report hit."""
    if "token" in e:
        return "token:" + e["token"]
    return e.get("glob") or e.get("path") or ""


def dedupe(entries):
    """First entry per key; an enforced entry replaces an earlier shadow entry of the same key."""
    order, seen = [], {}
    for e in entries:
        k = key(e)
        if k not in seen:
            seen[k] = e
            order.append(k)
        elif seen[k].get("shadow") and not e.get("shadow"):
            seen[k] = e
    return [seen[k] for k in order]


def removed_keys(remove):
    return {key(e) for e in expand(remove or [])}


def path_hit(e, path):
    """Does a glob or token entry match `path`?"""
    if "token" in e:
        return token_match(e["token"], path)
    return glob_dir_match(e.get("glob", ""), path)


def content_hit(rule, path, hunk):
    """The text of the first added or removed line of `path` that holds one of the rule's `contains` strings, else None."""
    if not glob_dir_match(rule.get("path", ""), path):
        return None
    wanted = contains_list(rule)
    for _, text in list((hunk or {}).get("added", [])) + list((hunk or {}).get("removed", [])):
        for w in wanted:
            if w and w in text:
                return w
    return None


def layer_of(e, floor_keys):
    """org = declared by the organization or the project, default = shipped floor, content = stack preset or content rule."""
    src = str(e.get("source") or "")
    if src.startswith("preset:") or "contains" in e:
        return "content"
    if src.startswith("codeowners:") or src == "add":
        return "org"
    return "default" if key(e) in floor_keys else "org"


# ---------------------------------------------------------------- CODEOWNERS (GitHub semantics, docs.github.com about-code-owners)
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")     # first file found wins (GitHub: .github/, root, docs/)


def owner_looks_security(owner):
    toks = [t.lower() for t in OWNER_SPLIT.split(owner) if t]
    for t in toks:
        if t in OWNER_WORDS or t.startswith(OWNER_PREFIX_WORDS):
            return True
    return False


def codeowners_glob(pattern):
    """CODEOWNERS pattern -> agentive glob, or None when it cannot be converted (negation, character range, escape).

    Table [design judgment, from the GitHub description of CODEOWNERS: a pattern with a leading slash is anchored at the root, a name
    without a slash matches at any depth, a trailing slash means a directory and everything below]:
      /x/ -> x/**   /x -> x   x/ -> **/x/**   x -> **/x   *.ext -> **/*.ext   a/b -> a/b (an interior slash anchors it)
      **/x -> **/x  a/** -> a/**
    """
    p = pattern.strip()
    if not p or p.startswith("!") or "[" in p or "\\" in p:
        return None
    anchored = p.startswith("/")
    if anchored:
        p = p.lstrip("/")
    is_dir = p.endswith("/")
    if is_dir:
        p = p.rstrip("/")
    if not p:
        return None
    if p.startswith("**/") or p.endswith("/**") or p == "**":
        return p
    base = p + "/**" if is_dir else p
    if anchored or "/" in p:
        return base
    return "**/" + base


def parse_codeowners(text):
    """-> {entries: [{glob, reason: security, source: codeowners:<line>}], owned: n, skipped: [(line, pattern, why)]}.

    Only lines with a security-looking owner become entries (decision SECPATHS L1); the other lines are counted."""
    entries, owned, skipped, seen = [], 0, [], set()
    for no, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue                       # a pattern without an owner removes ownership; nothing to snapshot
        pattern, owners = parts[0], parts[1:]
        owned += 1
        if not any(owner_looks_security(o) for o in owners):
            continue
        g = codeowners_glob(pattern)
        if g is None:
            skipped.append((no, pattern, "pattern not convertible"))
            continue
        if g in seen:
            continue
        seen.add(g)
        entries.append({"glob": g, "reason": "security", "source": f"codeowners:{no}"})
    return {"entries": entries, "owned": owned, "skipped": skipped}
