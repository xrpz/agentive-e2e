# agentive-unit: lib.agentive
# design: spec.context_management.context_gate, spec.verb_wrapper.rewrite · owner B
"""Context gate (PreToolUse Read|Bash) and test-command rewrite (PreToolUse Bash, allow + updatedInput).

Context gate: a Read without offset/limit of a file larger than manifest context.read_limit_bytes (default
65,536 bytes) is denied, the Read tool's allow_large is always denied, and an unbounded cat/nl/bat of such a file
(not piped into another command) is denied. head, tail and sed -n are allowed. Generated and vendor paths are
already denied by the permissions.deny Read rules.
Test rewrite: a Bash command that is the manifest test command (optionally with extra arguments) or a bare known
test-runner invocation of the detected ecosystem is rewritten to `.agents/bin/verb test [-- args]` [design judgment:
only exact or prefix matches, so arguments are kept and nothing else is rewritten].
"""
import os
import shlex

from . import guard

__all__ = ["context_gate", "test_rewrite", "DEFAULT_LIMIT", "KNOWN_RUNNERS"]

DEFAULT_LIMIT = 65536
REASON = "use offset/limit, head/tail/sed -n or Grep"
KNOWN_RUNNERS = ["npm test", "npm run test", "pnpm test", "pnpm run test", "yarn test", "yarn run test", "bun test",
                 "bun run test", "npx jest", "npx vitest run", "pytest", "python -m pytest", "python3 -m pytest",
                 "uv run pytest", "poetry run pytest", "go test ./...", "cargo test", "mvn test", "mvn -B test",
                 "./mvnw test", "gradle test", "./gradlew test", "make test"]


def limit_of(manifest):
    try:
        return int(((manifest or {}).get("context") or {}).get("read_limit_bytes") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _size(path):
    try:
        return os.path.getsize(path) if os.path.isfile(path) else -1
    except OSError:
        return -1


def context_gate(tool_name, tool_input, cwd, manifest):
    """None (no decision) or a deny reason string."""
    limit = limit_of(manifest)
    ti = tool_input or {}
    if tool_name == "Read":
        if ti.get("allow_large"):
            return f"BLOCKED context-gate: Read allow_large is always denied. NEXT: {REASON}"
        if ti.get("offset") is None and ti.get("limit") is None:
            p = ti.get("file_path") or ti.get("path") or ""
            p = p if os.path.isabs(p) else os.path.join(cwd, p)
            n = _size(p)
            if n > limit:
                return f"BLOCKED context-gate: {ti.get('file_path')} is {n} bytes (> {limit}) and the Read has no offset/limit. NEXT: {REASON}"
        return None
    if tool_name in ("Bash", "PowerShell"):
        segs = guard.segments(ti.get("command") or "")
        for i, (seg, _op) in enumerate(segs):
            argv, _ = guard.normalize(seg)
            if not argv or os.path.basename(argv[0]) not in ("cat", "nl", "bat", "batcat"):
                continue
            piped = i + 1 < len(segs) and segs[i + 1][1] == "|"
            if piped:
                continue
            for a in argv[1:]:
                if a.startswith("-") or a == "<":
                    continue
                p = a if os.path.isabs(a) else os.path.join(cwd, a)
                n = _size(p)
                if n > limit:
                    return f"BLOCKED context-gate: {argv[0]} of {a} ({n} bytes > {limit}) is unbounded. NEXT: {REASON}"
    return None


def _norm(s):
    return " ".join(s.split())


def test_rewrite(command, manifest):
    """The rewritten command, or None."""
    spec = ((manifest or {}).get("verbs") or {}).get("test")
    if not isinstance(spec, dict) or not spec.get("cmd") or spec.get("status") == "pending":
        return None
    cmd = _norm(command or "")
    if not cmd or len(guard.segments(cmd)) != 1:
        return None
    tcmd = _norm(spec["cmd"])
    if cmd == tcmd:
        return ".agents/bin/verb test"
    if cmd.startswith(tcmd + " "):
        rest = cmd[len(tcmd) + 1:]
        try:
            args = shlex.split(rest)
        except ValueError:
            return None
        return ".agents/bin/verb test -- " + " ".join(shlex.quote(a) for a in args)
    if cmd in KNOWN_RUNNERS:
        return ".agents/bin/verb test"
    return None
