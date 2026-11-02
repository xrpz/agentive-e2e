#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.guard, spec.enforcement.hooks.pre_tool_use_guard, spec.no_human_run, spec.handoff.writers, spec.spec_stage.chain.flow, decisions.accepted[PLANMODEGATE], spec.guard.push_without_refspec
"""PreToolUse guard (matcher Bash|PowerShell; Codex: Bash). Claude Code registers it with onFailure: block.

chain-plan gate (Claude Code, E2E-18) -> the Bash call running .agents/skills/chain-plan/scripts/stage-check.sh outside
              plan mode (permission_mode of this call's input != plan) is denied with `AGENTIVE_CHAIN=stop:plan:<slug> NEXT=...`
push gate (E2E-36) -> with no human present a `git push` without a refspec is denied (`NEXT: git push -u origin HEAD`)

deny rule  -> permissionDecision deny + `BLOCKED <id>: <reason>. NEXT: <next>` (every mode, never ask)
ask rule   -> human present: no decision (the permissions.ask rule prompts; Codex: .codex/rules prompt)
              no human (no_human_run): deny with a handoff reason + handoff file (reason ask_class_denied)
Hooks never return ask (Codex PreToolUse ask is fail-open).
"""
import os
import sys

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import gates, guard, handoff, hookio, nohuman  # noqa: E402


RESTORE = {"git checkout -- .agents/guard.yaml", "git restore .agents/guard.yaml", "git restore -- .agents/guard.yaml"}


def main(argv):
    tool = "codex" if hookio.tool_arg(argv) == "codex" else "claude_code"
    inp = hookio.read_input()
    cmd = (inp.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return 0
    stop = gates.chain_plan_gate(cmd, inp, tool, no_human=nohuman.no_human(hook_input=inp)) or \
        gates.headless_push_gate(cmd, nohuman.no_human(hook_input=inp))
    if stop:
        hookio.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": stop}})
        return 0
    root = hookio.root_from(inp)
    cwd = inp.get("cwd") if os.path.isdir(inp.get("cwd") or "") else root
    try:
        rules = guard.load_rules(root)
    except Exception as e:  # noqa: BLE001 - a missing or broken guard.yaml must block with a NEXT, not crash (V1-17)
        if " ".join(cmd.split()) in RESTORE:
            return 0        # the one way back with Bash: restore the committed guard.yaml
        why = "missing" if isinstance(e, OSError) else f"unreadable ({str(e).splitlines()[0][:160] if str(e) else type(e).__name__})"
        hookio.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason":
                     f"BLOCKED guard-config: .agents/guard.yaml is {why}, so no command can be judged. "
                     "NEXT: run `git checkout -- .agents/guard.yaml` (the only command allowed now) or restore it with the Edit tool, "
                     "or ask the human to run /agentive:update (Codex: $agentive-update)"}})
        return 0
    rule, text = guard.evaluate(cmd, rules, root, cwd, tool)
    if rule is None:
        return 0
    if rule["decision"] == "ask" and not nohuman.no_human(hook_input=inp):
        return 0
    extra = ""
    if rule["decision"] == "ask":
        path = handoff.write(root, "ask_class_denied",
                             failure_reason=f"guard rule {rule['id']} needs a human: {rule['reason']}",
                             next_step=rule["next"], blocked_command=f"{text} (rule {rule['id']})")
        extra = f" (no human present; handed off in {path})"
    hookio.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": guard.block_message(rule, extra)}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
