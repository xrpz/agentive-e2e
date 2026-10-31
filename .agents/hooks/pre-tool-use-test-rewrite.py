#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.verb_wrapper.rewrite, spec.enforcement.hooks.pre_tool_use_test_rewrite
"""PreToolUse test rewrite (matcher Bash): a raw test-runner command becomes `.agents/bin/verb test`
(permissionDecision allow + updatedInput, the form both Claude Code and Codex accept)."""
import os
import sys

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import gates, hookio  # noqa: E402


def main(argv):
    inp = hookio.read_input()
    ti = inp.get("tool_input") or {}
    cmd = ti.get("command")
    if not isinstance(cmd, str):
        return 0
    root = hookio.root_from(inp)
    new = gates.test_rewrite(cmd, hookio.load_manifest(root))
    if not new:
        return 0
    updated = dict(ti, command=new)
    hookio.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                        "permissionDecisionReason": f"agentive: test runs through the verb wrapper ({new})",
                                        "updatedInput": updated}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
