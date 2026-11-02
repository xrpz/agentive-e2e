#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.context_management.context_gate, spec.enforcement.hooks.pre_tool_use_context_gate, spec.context_management.headless.bash_hint, decisions.accepted[HEADLESSHINT]
"""PreToolUse context gate (matcher Read|Bash; Codex: Bash): deny unbounded reads of files above
manifest context.read_limit_bytes (default 64 KiB) and every Read with allow_large.

Headless Bash hint (Claude Code, E2E-32, HEADLESSHINT): with no human present, a Bash call that the headless allowlist would
refuse gets additionalContext naming the part and the alternative (agentive.headless). The hint never decides: it adds no
permissionDecision, so the call is allowed or refused exactly as without it. It lives in this hook because the guard hook is
registered with onFailure: block, and a bug in a hint must never block Bash [design judgment]."""
import os
import sys

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import gates, hookio  # noqa: E402


def _hint(inp, tool):
    if tool != "claude_code" or inp.get("tool_name") != "Bash":
        return None
    try:
        from agentive import headless, nohuman
        return headless.hint((inp.get("tool_input") or {}).get("command"), nohuman.no_human(hook_input=inp), tool)
    except Exception:  # noqa: BLE001 - a hint is optional; it never fails the hook
        return None


def main(argv):
    tool = "codex" if hookio.tool_arg(argv) == "codex" else "claude_code"
    inp = hookio.read_input()
    root = hookio.root_from(inp)
    cwd = inp.get("cwd") if os.path.isdir(inp.get("cwd") or "") else root
    reason = gates.context_gate(inp.get("tool_name") or "", inp.get("tool_input") or {}, cwd, hookio.load_manifest(root))
    out = {"hookEventName": "PreToolUse"}
    if reason:
        out.update(permissionDecision="deny", permissionDecisionReason=reason)
    hint = _hint(inp, tool)
    if hint:
        out["additionalContext"] = hint
    if len(out) > 1:
        hookio.emit({"hookSpecificOutput": out})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
