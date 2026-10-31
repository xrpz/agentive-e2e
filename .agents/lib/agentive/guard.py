# agentive-unit: lib.agentive
# design: spec.guard, spec.enforcement.permissions, contracts[guard-rules], spec.repo_layer.files[f-codex-rules] · owner B
"""Guard rules (.agents/guard.yaml): normalize a shell command, match deny/ask rules, render permission rules.

Readers: .agents/hooks/pre-tool-use-guard.py (Claude Code and Codex) and the apply engine (renders
.claude/settings.json permissions.deny/ask, .codex/rules/agentive.rules and its check fixture).

Match kinds (schema guard-rules) [design judgment for the value forms]:
- argv_prefix: `value` is a list of patterns; each pattern is a space-separated argv prefix in Claude permission
  syntax where `*` matches any characters (`git clean -f*`, `git push * --delete*`). A command matches when its
  normalized text equals the pattern or starts with it followed by a space (an implicit trailing ` *`).
  Rendered as `Bash(<pattern>)` + `Bash(<pattern> *)`; Codex prefix_rule only for patterns without `*`.
- regex: Python regex searched in the normalized command text (hook only).
- special: named checks implemented here: default_branch_push, tag_push, push_delete_refspec, unresolvable_rm_var,
  sql_without_where, sql_destructive (hook only).
- unless: argv tokens (exact) or `re:<regex>` that exempt the command; a rule with unless is hook only, because
  permissions.deny cannot carve exceptions (spec.guard.deny[publish].placement).
Normalization (spec.guard.normalize): split on && || ; | & and newlines outside quotes; strip leading env
assignments and env/command/exec/nohup/time/builtin; `git -C <dir>`, `-c k=v`, `--git-dir`, `--work-tree`,
`--no-pager` are dropped; one level of `sh|bash|zsh -c '<cmd>'` is unwrapped. The default branch comes from
origin/HEAD (fallback main and master), a bare `git push` means the current branch.
Hooks see only command text: a guardrail, not a security boundary (spec.guard.boundary).
"""
import os
import re
import shlex
import subprocess

from . import ayaml

__all__ = ["load_rules", "segments", "normalize", "evaluate", "claude_permissions", "codex_rules",
           "codex_check_lines", "block_message", "validate_override", "merge_rules", "SQL_CLIENTS"]

WRAPPERS = {"env", "command", "exec", "nohup", "time", "builtin", "nice"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "clickhouse-client", "duckdb", "cockroach", "mongosh"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# ------------------------------------------------------------------ rules
def load_rules(root):
    """Rules of the installed .agents/guard.yaml (already merged with .agents/custom/ by the engine)."""
    path = os.path.join(root, ".agents", "guard.yaml")
    doc = ayaml.load(path)
    rules = doc.get("rules") or []
    for r in rules:
        r.setdefault("expressible", True)
        r.setdefault("tools", ["claude_code", "codex"])
        r.setdefault("source", "framework")
    return rules


def validate_override(base_rules, override_rules):
    """Problems of a repo override (spec.guard.repo_rules): it may add rules or turn ask into deny, never remove
    a framework rule, turn deny into ask or change how a framework rule matches. Returns a list of messages."""
    errs = []
    base = {r["id"]: r for r in base_rules}
    for o in override_rules or []:
        if not isinstance(o, dict) or "id" not in o:
            errs.append("every override rule needs an id")
            continue
        b = base.get(o["id"])
        if b is None:
            for k in ("decision", "match", "reason", "next"):
                if k not in o:
                    errs.append(f"new rule {o['id']}: missing {k}")
            if o.get("source", "repo") != "repo":
                errs.append(f"new rule {o['id']}: source must be repo")
            continue
        for k, v in o.items():
            if k == "id":
                continue
            if k == "decision":
                if b["decision"] == "deny" and v != "deny":
                    errs.append(f"rule {o['id']}: deny cannot become {v}")
                continue
            if k in ("reason", "next") and isinstance(v, str) and v:
                continue
            errs.append(f"rule {o['id']}: field {k} of a framework rule cannot be changed (only decision ask -> deny, reason, next)")
    return errs


def merge_rules(base_rules, override_rules):
    out = [dict(r) for r in base_rules]
    idx = {r["id"]: i for i, r in enumerate(out)}
    for o in override_rules or []:
        if o["id"] in idx:
            out[idx[o["id"]]].update({k: v for k, v in o.items() if k != "id"})
        else:
            n = dict(o)
            n["source"] = "repo"
            out.append(n)
    return out


def _sentence(text):
    """`text` without its one closing period. Only a single sentence-ending '.' goes: a reason that ends in a literal
    `..` (agentive-path-traversal) or `./...` keeps it (E2E-05: rstrip('.') printed "leaves it through . NEXT")."""
    text = text.rstrip()
    if text.endswith(".") and not text.endswith(".."):
        return text[:-1]
    return text


def block_message(rule, extra=""):
    return f"BLOCKED {rule['id']}: {_sentence(rule['reason'])}{extra}. NEXT: {rule['next']}"


# ------------------------------------------------------------------ normalization
def segments(cmd):
    """Split a shell command into simple-command segments, outside quotes. Returns [(text, op_before)]."""
    out, buf, q, i, op = [], [], None, 0, ""
    while i < len(cmd):
        c = cmd[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "'\"":
            q = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < len(cmd):
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            out.append(("".join(buf).strip(), op))
            buf, op = [], two
            i += 2
            continue
        if c in ";|&\n":
            if c == "&" and i + 1 < len(cmd) and cmd[i + 1] == ">":   # &> redirection
                buf.append(c)
                i += 1
                continue
            if c == "|" and i > 0 and cmd[i - 1] == ">":
                buf.append(c)
                i += 1
                continue
            out.append(("".join(buf).strip(), op))
            buf, op = [], c
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append(("".join(buf).strip(), op))
    return [(t, o) for t, o in out if t]


def _split(text):
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:
        return text.split()


def normalize(text):
    """argv after stripping env assignments, wrappers and git global options. Returns (argv, inner_command|None)."""
    argv = _split(text)
    while argv:
        if _ASSIGN.match(argv[0]):
            argv = argv[1:]
            continue
        if argv[0] in WRAPPERS:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
            continue
        break
    # redirections are not part of argv for matching
    argv = [a for a in argv if not re.match(r"^\d*[<>]{1,2}&?\d*$", a)]
    if argv and os.path.basename(argv[0]) in SHELLS:
        rest = argv[1:]
        for j, a in enumerate(rest):
            if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                if j + 1 < len(rest):
                    return argv, rest[j + 1]
                break
            if not a.startswith("-"):
                break
    if argv and argv[0] == "git":
        out, j = ["git"], 1
        while j < len(argv):
            a = argv[j]
            if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                j += 2
                continue
            if a.startswith(("--git-dir=", "--work-tree=", "--namespace=", "-c")) and a != "-c":
                j += 1
                continue
            if a in ("--no-pager", "-P", "--paginate", "-p", "--bare", "--no-replace-objects", "--literal-pathspecs"):
                j += 1
                continue
            break
        argv = out + argv[j:]
    return argv, None


def _glob_re(pattern):
    parts = [re.escape(p) for p in pattern.split("*")]
    return re.compile("^" + ".*".join(parts) + "$", re.S)


def _prefix_match(pattern, text):
    rx = _glob_re(pattern)
    return bool(rx.match(text) or (not pattern.endswith("*") and _glob_re(pattern + " *").match(text)))


def _exempt(rule, argv, text):
    for u in rule["match"].get("unless") or []:
        if u.startswith("re:"):
            if re.search(u[3:], text):
                return True
        elif u in argv:
            return True
    return False


# ------------------------------------------------------------------ specials
def _git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _default_branches(cwd):
    ref = _git(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return {ref.split("/", 1)[1]}
    return {"main", "master"}


_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec", "--signed"}


def _push_parts(argv):
    """(flags, remote, refspecs) of a `git push` argv."""
    flags, pos, j = [], [], 2
    while j < len(argv):
        a = argv[j]
        if a == "--":
            pos += argv[j + 1:]
            break
        if a.startswith("-"):
            flags.append(a)
            if a in _PUSH_OPTS_WITH_VALUE:
                j += 1
        else:
            pos.append(a)
        j += 1
    return flags, (pos[0] if pos else None), pos[1:]


def _is_push(argv):
    return len(argv) >= 2 and argv[0] == "git" and argv[1] == "push"


def _special_default_branch_push(argv, ctx):
    if not _is_push(argv):
        return False
    flags, _remote, refs = _push_parts(argv)
    if "--all" in flags or "--branches" in flags:
        return True
    if any(f in ("--delete", "-d", "--mirror", "--tags", "--prune") for f in flags):
        return False   # delete/mirror/prune are ask rules; --tags is push-tag
    defaults = _default_branches(ctx["cwd"])
    current = _git(ctx["cwd"], "symbolic-ref", "--quiet", "--short", "HEAD")
    targets = []
    if not refs:
        targets.append(current)
    for spec in refs:
        spec = spec.lstrip("+")
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        if dst in ("", None):
            continue
        if dst == "HEAD":
            dst = current
        if dst and dst.startswith("refs/heads/"):
            dst = dst[len("refs/heads/"):]
        targets.append(dst)
    return any(t in defaults for t in targets if t)


def _special_tag_push(argv, ctx):
    if not _is_push(argv):
        return False
    flags, _remote, refs = _push_parts(argv)
    if "--tags" in flags or "--follow-tags" in flags:
        return True
    for spec in refs:
        for side in spec.lstrip("+").split(":"):
            if side.startswith("refs/tags/"):
                return True
            if side and _git(ctx["cwd"], "show-ref", "--verify", "--quiet", f"refs/tags/{side}") is not None \
                    and _git(ctx["cwd"], "show-ref", "--verify", "--quiet", f"refs/heads/{side}") is None:
                return True
    return False


_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)|\$\(|`")


def _assigned(full):
    names = set(re.findall(r"(?:^|[\s;&|(])(?:export\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=", full))
    names |= set(re.findall(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", full))
    names |= set(re.findall(r"\bread\s+(?:-[a-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)", full))
    return names


def _special_push_delete_refspec(argv, ctx):
    """`git push <remote> :<ref>` (also `+:<ref>`): a refspec with an empty source deletes the remote ref (V1-09)."""
    if not _is_push(argv):
        return False
    _flags, _remote, refs = _push_parts(argv)
    return any(spec.lstrip("+").startswith(":") and len(spec.lstrip("+")) > 1 for spec in refs)


def _special_unresolvable_rm_var(argv, ctx):
    if not argv or os.path.basename(argv[0]) != "rm":
        return False
    recursive = any(a in ("--recursive",) or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"r", "R"}) for a in argv[1:])
    targets = [a for a in argv[1:] if not a.startswith("-")]
    raw_targets = [a for a in _split_raw(ctx["segment"])[1:] if not a.startswith("-")]
    if recursive:
        assigned = _assigned(ctx["full"])
        for t in raw_targets:
            for m in _VAR.finditer(t):
                if m.group(0) in ("$(", "`") or m.group(1) not in assigned:
                    return True
    root = os.path.realpath(ctx["root"])
    for t in targets:
        if _VAR.search(t):
            continue
        p = os.path.expanduser(t)
        p = p if os.path.isabs(p) else os.path.join(ctx["cwd"], p)
        if any(ch in os.path.basename(p) for ch in "*?["):
            p = os.path.dirname(p)
        rp = os.path.realpath(p)
        if rp == root or root.startswith(rp.rstrip(os.sep) + os.sep) or rp == os.sep:
            return True
    return False


def _split_raw(text):
    """shlex split that keeps quoting characters out but keeps $VAR text (shlex never expands)."""
    return _split(text)


def _sql_text(ctx):
    for seg, _op in segments(ctx["full"]):
        a, _ = normalize(seg)
        if a and os.path.basename(a[0]) in SQL_CLIENTS:
            return ctx["full"]
    return None


def _special_sql_without_where(argv, ctx):
    text = _sql_text(ctx)
    if not text:
        return False
    for m in re.finditer(r"(?is)\bdelete\s+from\b", text):
        tail = re.split(r";|\"|'|\n", text[m.end():], maxsplit=1)[0]
        if not re.search(r"(?i)\bwhere\b", tail):
            return True
    return False


def _special_sql_destructive(argv, ctx):
    text = _sql_text(ctx)
    return bool(text and re.search(r"(?i)\bdrop\s+(database|schema)\b", text))


_COMMIT_VALUE_OPTS = {"-m", "-F", "-c", "-C", "-t", "--author", "--date", "--message", "--file", "--template",
                      "--reuse-message", "--reedit-message", "--fixup", "--squash", "--trailer", "--cleanup"}


def _special_no_verify(argv, ctx):
    if len(argv) < 2 or argv[0] != "git" or argv[1] not in ("commit", "push"):
        return False
    skip = False
    for a in argv[2:]:
        if skip:
            skip = False
            continue
        if a == "--no-verify":
            return True
        if argv[1] == "commit":
            if a in _COMMIT_VALUE_OPTS:
                skip = True
                continue
            if re.match(r"^-[a-zA-Z]+$", a):
                letters = a[1:]
                if "n" in letters.split("m")[0].split("F")[0].split("c")[0].split("C")[0]:
                    return True
                if letters[-1] in "mFcCt":
                    skip = True
    return False


SPECIALS = {"no_verify": _special_no_verify, "default_branch_push": _special_default_branch_push, "tag_push": _special_tag_push,
            "unresolvable_rm_var": _special_unresolvable_rm_var, "sql_without_where": _special_sql_without_where,
            "sql_destructive": _special_sql_destructive, "push_delete_refspec": _special_push_delete_refspec}


# ------------------------------------------------------------------ evaluation
def _rule_hits(rule, argv, text, ctx):
    m = rule["match"]
    kind, value = m["kind"], m["value"]
    if kind == "argv_prefix":
        pats = value if isinstance(value, list) else [value]
        hit = any(_prefix_match(p, text) for p in pats)
    elif kind == "regex":
        pats = value if isinstance(value, list) else [value]
        hit = any(re.search(p, text) for p in pats)
    elif kind == "special":
        names = value if isinstance(value, list) else [value]
        hit = any(SPECIALS[n](argv, ctx) for n in names if n in SPECIALS)
    else:
        hit = False
    return hit and not _exempt(rule, argv, text)


def _walk(cmd, depth=0):
    """Yield (segment_text, argv, normalized_text) for every simple command, unwrapping one `sh -c` level."""
    for seg, _op in segments(cmd):
        argv, inner = normalize(seg)
        if inner is not None and depth == 0:
            yield from _walk(inner, depth + 1)
            continue
        if argv:
            yield seg, argv, " ".join(argv)


def evaluate(command, rules, root, cwd=None, tool="claude_code"):
    """The strongest matching rule for a command: (rule, normalized segment) with deny before ask, or (None, None)."""
    cwd = cwd or root
    ctx = {"root": root, "cwd": cwd, "full": command}
    best = None
    for seg, argv, text in _walk(command):
        ctx["segment"] = seg
        for r in rules:
            if tool not in r.get("tools", ["claude_code", "codex"]):
                continue
            if _rule_hits(r, argv, text, ctx):
                if r["decision"] == "deny":
                    return r, text
                best = best or (r, text)
    return best if best else (None, None)


# ------------------------------------------------------------------ rendering
def _expressible_patterns(rule, tool):
    m = rule["match"]
    if m["kind"] != "argv_prefix" or m.get("unless") or tool not in rule.get("tools", ["claude_code", "codex"]):
        return []
    return m["value"] if isinstance(m["value"], list) else [m["value"]]


def claude_permissions(rules):
    """(deny, ask) Bash permission rules in normalized form (spec.guard.renders[0])."""
    deny, ask = [], []
    for r in rules:
        dest = deny if (r["decision"] == "deny" or not r.get("expressible", True)) else ask
        for p in _expressible_patterns(r, "claude_code"):
            for form in ([f"Bash({p})"] if p.endswith("*") else [f"Bash({p})", f"Bash({p} *)"]):
                if form not in dest:
                    dest.append(form)
    return deny, ask


def _starlark(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_rules(rules):
    """Text of .codex/rules/agentive.rules (experimental execpolicy prefix_rule; spec.repo_layer.files[f-codex-rules])."""
    out = []
    for r in rules:
        decision = "forbidden" if (r["decision"] == "deny" or not r.get("expressible", True)) else "prompt"
        for p in _expressible_patterns(r, "codex"):
            if "*" in p:
                continue
            argv = p.split()
            pat = "[" + ", ".join(_starlark(a) for a in argv) + "]"
            out += ["prefix_rule(",
                    f"    pattern = {pat},",
                    f"    decision = {_starlark(decision)},",
                    f"    justification = {_starlark(block_message(r))},",
                    f"    match = [{pat}],",
                    ")", ""]
    return "\n".join(out).rstrip("\n")


def codex_check_lines(rules):
    """Fixture lines `<decision>\\t<argv...>` for `codex execpolicy check` (one per rendered prefix_rule)."""
    out = []
    for r in rules:
        decision = "forbidden" if (r["decision"] == "deny" or not r.get("expressible", True)) else "prompt"
        for p in _expressible_patterns(r, "codex"):
            if "*" not in p:
                out.append(f"{decision}\t{r['id']}\t{p}")
    return "\n".join(out)


def not_expressible(rules):
    """Ask items no permission rule can express: rendered as deny, init warns (spec.guard.decisions.not_expressible)."""
    return [r for r in rules if not r.get("expressible", True)]

