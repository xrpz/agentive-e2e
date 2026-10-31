#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.maintenance.harvest, contracts[harvest-queue]
"""SessionEnd harvest: append one line to .agents/state/harvest-queue.jsonl when a signal fired (<= 100 ms).

Signals (constants, N-D8): stop_blocks >= 3 in the session (stop.json total_blocks), verify_fail_streak >= 2
(trailing failing runs in verify.json history), repeated_command: a normalized Bash command seen >= 2 times in
this session's transcript (git status, ls, pwd and the test verb excluded). Cross-session repetition is left to
/distill, which reads every queue line [design judgment: no extra state file]. Nothing fired -> nothing written.
"""
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import common, hookio, state  # noqa: E402

EXCLUDE = re.compile(r"^(git status|ls|pwd|\.agents/bin/verb test)(\s|$)")
MAX_BYTES = 4 * 1024 * 1024


def _norm(cmd):
    cmd = " ".join(cmd.split())
    cmd = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+", "", cmd)
    return cmd


def _commands(transcript, deadline):
    out = []
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            if size > MAX_BYTES:
                f.seek(size - MAX_BYTES)
                f.readline()
            for raw in f:
                if time.monotonic() > deadline:
                    break
                if b'"Bash"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                content = ((obj.get("message") or {}).get("content")) if isinstance(obj, dict) else None
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                        cmd = (c.get("input") or {}).get("command")
                        if isinstance(cmd, str) and cmd.strip():
                            out.append(_norm(cmd))
    except OSError:
        pass
    return out


def main(argv):
    deadline = time.monotonic() + 0.08
    inp = hookio.read_input()
    root = hookio.root_from(inp)
    if hookio.load_manifest(root) is None:
        return 0
    sid = inp.get("session_id") or ""
    signals = {}
    s = state.load_stop(root, sid)
    if int(s.get("total_blocks") or 0) >= 3:
        signals["stop_blocks"] = int(s["total_blocks"])
    hist = state.load_verify(root).get("history") or []
    streak = 0
    for h in reversed(hist):
        if int(h.get("total_failures") or 0) > 0:
            streak += 1
        else:
            break
    if streak >= 2:
        signals["verify_fail_streak"] = streak
    tp = inp.get("transcript_path") or ""
    if tp and os.path.isfile(tp):
        counts = {}
        for c in _commands(tp, deadline):
            if not EXCLUDE.match(c):
                counts[c] = counts.get(c, 0) + 1
        rep = [{"normalized": c, "count": n, "sessions": 1} for c, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if n >= 2][:10]
        if rep:
            signals["repeated_command"] = rep
    if not signals:
        return 0
    state.append_harvest(root, {"session_id": sid, "transcript_path": tp, "reason": str(inp.get("reason") or "other"),
                                "signals": signals, "at": common.utc_now()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
