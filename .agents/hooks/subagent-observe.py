#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.work_loop.precommit_review.attestation, spec.work_loop.independence, decisions.accepted[INDEPCI]
"""SubagentStart and SubagentStop (Claude Code only): observe that a subagent really ran.

Appends one line {event, session_id, agent_id, agent_type, tool, ts} to .agents/state/subagents.jsonl and nothing
else. `.agents/bin/agentive-review precommit` matches a review task with the line a hook wrote for the same
agent type in the task's time window: only then the local review record says attestation hook_observed and
context isolated (docs: hook input carries agent_id and agent_type for a subagent [S2]). The hook never blocks,
prints nothing and keeps the file small (the newest 200 lines once it passes 128 KB). Claude Code only: the
Codex SubagentStart and SubagentStop input fields are unverified, so Codex local reviews stay executed_by unknown,
context unverified [design judgment].
"""
import json
import os
import sys
import time

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import hookio  # noqa: E402

MAX_BYTES = 128 * 1024
KEEP_LINES = 200


def main(argv):
    inp = hookio.read_input()
    root = hookio.root_from(inp)
    if not os.path.isfile(os.path.join(root, ".agents", "manifest.yaml")):
        return 0
    name = str(inp.get("hook_event_name") or "")
    event = "start" if name == "SubagentStart" else ("stop" if name == "SubagentStop" else "")
    if not event:
        return 0
    rec = {"event": event, "session_id": str(inp.get("session_id") or ""), "agent_id": str(inp.get("agent_id") or ""),
           "agent_type": str(inp.get("agent_type") or ""), "tool": "codex" if hookio.tool_arg(argv) == "codex" else "claude_code",
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    path = os.path.join(hookio.state_dir(root), "subagents.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(path) > MAX_BYTES:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()[-KEEP_LINES:]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
