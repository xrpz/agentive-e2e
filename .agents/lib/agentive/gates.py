# agentive-unit: lib.agentive
# design: spec.context_management.context_gate, spec.verb_wrapper.rewrite, spec.spec_stage.chain.flow, decisions.accepted[PLANMODEGATE] · owner B
"""Context gate (PreToolUse Read|Bash), test-command rewrite (PreToolUse Bash, allow + updatedInput) and the chain-plan
plan-mode gate (PreToolUse Bash, called by the guard hook).

Context gate: a Read without offset/limit of a file larger than manifest context.read_limit_bytes (default
65,536 bytes) is denied, the Read tool's allow_large is always denied, and an unbounded cat/nl/bat of such a file
(not piped into another command) is denied. head, tail and sed -n are allowed. Generated and vendor paths are
already denied by the permissions.deny Read rules.
Test rewrite: a Bash command that is the manifest test command (optionally with extra arguments) or a bare known
test-runner invocation of the detected ecosystem is rewritten to `.agents/bin/verb test [-- args]` [design judgment:
only exact or prefix matches, so arguments are kept and nothing else is rewritten].
Chain-plan gate (E2E-18, PLANMODEGATE): on Claude Code the Bash call that runs the plan stage's stage-check.sh must
come from plan mode: the PreToolUse input of that very call carries permission_mode, and any value other than plan is
denied with the chain stop line. No field (Codex, an event without it) or another command: no decision; stage-check.sh
keeps its environment rule (AGENTIVE_HEADLESS, CI, GITHUB_ACTIONS) for those. This gate is not a guard.yaml rule.
Headless push gate (E2E-36, decision e2e_32_36): with no human present (headless runs (`claude -p`), Routines runs, CI) a
`git push` without a refspec is denied with `NEXT: git push -u origin HEAD`: its target comes from push.default,
remote.<name>.push and the branch's upstream, which the guard cannot see reliably (push.default=upstream sends a branch made
from origin/main to main). Interactive sessions are unchanged (reading those settings there comes later). Not a guard.yaml rule
either: guard.yaml rules do not know whether a human is present.
"""
import os
import shlex

from . import guard

__all__ = ["context_gate", "test_rewrite", "chain_plan_gate", "headless_push_gate", "DEFAULT_LIMIT", "KNOWN_RUNNERS", "PLAN_STAGE_CHECK"]

DEFAULT_LIMIT = 65536
REASON = "use offset/limit, head/tail/sed -n or Grep"
PLAN_STAGE_CHECK = ".agents/skills/chain-plan/scripts/stage-check.sh"
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


def _plan_stage_check_slug(command):
    """The slug argument when a simple command of `command` (one `sh -c` level unwrapped, env assignments stripped) runs
    the chain-plan stage-check.sh (`sh [-opts] <path> <slug>` or `<path> <slug>`, the path repo-root relative, ./ or
    absolute), else None; "none" when no slug is given."""
    for _seg, argv, _text in guard._walk(command or ""):
        args = list(argv)
        if os.path.basename(args[0]) in guard.SHELLS:
            args = args[1:]
            while args and args[0].startswith("-"):
                args = args[1:]
        if not args:
            continue
        path = args[0].replace("\\", "/")
        if path == PLAN_STAGE_CHECK or path.endswith("/" + PLAN_STAGE_CHECK):
            return (args[1] if len(args) > 1 else "none")[:80]
    return None


def chain_plan_gate(command, hook_input, tool, no_human=False):
    """None (no decision) or the deny reason: the chain stop line `AGENTIVE_CHAIN=stop:plan:<slug> NEXT=...`."""
    if tool != "claude_code" or not isinstance(hook_input, dict):
        return None
    mode = hook_input.get("permission_mode")
    if not isinstance(mode, str) or not mode or mode == "plan":
        return None
    slug = _plan_stage_check_slug(command)
    if slug is None:
        return None
    if no_human:
        return (f"AGENTIVE_CHAIN=stop:plan:{slug} NEXT=plan approval needs a human: run /chain-plan {slug} in an interactive "
                f"session (permission_mode={mode}, not plan)")
    return (f"AGENTIVE_CHAIN=stop:plan:{slug} NEXT=enter plan mode first (EnterPlanMode tool, or ask the human to press "
            f"Shift+Tab), then rerun sh {PLAN_STAGE_CHECK} {slug}; in a headless run (claude -p) or a Routines run, stop here: "
            f"plan approval needs a human (permission_mode={mode}, not plan)")


PUSH_ID = "push-no-refspec"
PUSH_NEXT = "git push -u origin HEAD"
_PUSH_TARGET_FLAGS = {"--all", "--branches", "--mirror", "--tags"}      # other rules judge these (push-default-branch, push-tag)


def headless_push_gate(command, no_human):
    """None or the deny reason `BLOCKED push-no-refspec: ... NEXT: git push -u origin HEAD` (E2E-36) for a refspec-less
    `git push` (`git push`, `git push origin`, `git push -u origin`) when no human is present."""
    if not no_human:
        return None
    for _seg, argv, _text in guard._walk(command or ""):
        if not guard._is_push(argv):
            continue
        flags, _remote, refs = guard._push_parts(argv)
        if refs or _PUSH_TARGET_FLAGS & set(flags):
            continue
        return (f"BLOCKED {PUSH_ID}: a git push without a branch in a headless run (claude -p) or a Routines run; its target "
                f"would come from push.default, remote.<name>.push and the branch's upstream (a branch made from origin/main can "
                f"push to main). NEXT: {PUSH_NEXT}")
    return None
