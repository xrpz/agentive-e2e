#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.context_management.context_gate, spec.enforcement.hooks.pre_tool_use_context_gate
"""PreToolUse context gate (matcher Read|Bash; Codex: Bash): deny unbounded reads of files above
manifest context.read_limit_bytes (default 64 KiB) and every Read with allow_large."""
import os
import sys

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import gates, hookio  # noqa: E402


def main(argv):
    inp = hookio.read_input()
    root = hookio.root_from(inp)
    cwd = inp.get("cwd") if os.path.isdir(inp.get("cwd") or "") else root
    reason = gates.context_gate(inp.get("tool_name") or "", inp.get("tool_input") or {}, cwd, hookio.load_manifest(root))
    if reason:
        hookio.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": reason}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
