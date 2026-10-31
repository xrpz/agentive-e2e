# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV2].default_globs, spec.review_pipeline.rules (owner D)
"""Shared helpers of the review pipeline (stdlib only).

glob_match(glob, path): gitignore-like globs on repo-relative POSIX paths:
  `**/` = zero or more directories, `/**` at the end = everything below, `*` and `?` never cross `/`,
  `[...]` character classes. A glob without `/` still matches only from the repo root (`*.md` = root files)
  except that a leading `**/` is implied for none: write `**/x` for "anywhere" [design judgment: one rule, no implicit
  basename matching, so a glob means the same thing for RV2, RV3 and agentive-lint].
fenced_blocks(text, info): bodies of ``` fenced blocks whose info string is `info` (for example
  "yaml agentive-review-rules").
"""
import datetime
import hashlib
import json
import re

_GLOB_CACHE = {}


def _glob_regex(glob):
    i, n, out = 0, len(glob), ["^"]
    while i < n:
        c = glob[i]
        if c == "*":
            if glob[i:i + 2] == "**":
                j = i + 2
                if j < n and glob[j] == "/":
                    out.append("(?:.*/)?")          # **/ : zero or more directories
                    i = j + 1
                    continue
                out.append(".*")                     # ** at the end or before a name: anything
                i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def glob_match(glob, path):
    rx = _GLOB_CACHE.get(glob)
    if rx is None:
        rx = _GLOB_CACHE[glob] = _glob_regex(glob)
    return bool(rx.match(path))


def first_match(globs, path):
    for g in globs or []:
        if glob_match(g, path):
            return g
    return None


def fenced_blocks(text, info):
    """Bodies of fenced code blocks opened with ```<info> (exact info string, surrounding spaces ignored)."""
    out, lines, i = [], text.splitlines(), 0
    want = info.strip()
    while i < len(lines):
        m = re.match(r"^(\s*)(`{3,}|~{3,})\s*(.*?)\s*$", lines[i])
        if m and m.group(3) == want:
            fence, ind = m.group(2), len(m.group(1))
            body = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", lines[i]):
                body.append(lines[i][ind:] if lines[i][:ind].strip() == "" else lines[i])
                i += 1
            out.append("\n".join(body) + "\n")
        i += 1
    return out


def replace_fenced_block(text, info, new_body):
    """Replace the first ```<info> block body with new_body; append a new block when there is none."""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)(`{3,})\s*(.*?)\s*$", ln.rstrip("\n"))
        if m and m.group(3) == info:
            for j in range(i + 1, len(lines)):
                if re.match(r"^\s*`{3,}\s*$", lines[j]):
                    return "".join(lines[:i + 1]) + new_body.rstrip("\n") + "\n" + "".join(lines[j:])
    sep = "" if text.endswith("\n") or not text else "\n"
    return text + sep + "\n```" + info + "\n" + new_body.rstrip("\n") + "\n```\n"


def stable_id(*parts, prefix="F", n=10):
    h = hashlib.sha256("\0".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{h[:n]}"


def canonical_sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    """RFC 3339 / ISO 8601 (GitHub `2026-10-27T12:00:00Z`, git `%cI`) -> aware datetime."""
    s = s.strip().replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(s)


def uniq(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
