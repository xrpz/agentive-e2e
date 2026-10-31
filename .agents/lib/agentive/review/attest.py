# agentive-unit: lib.agentive
# design: spec.work_loop.precommit_review.attestation, spec.work_loop.ci_independence_check, spec.work_loop.independence, decisions.accepted[INDEPCI] (owner D)
"""Review records that say only what was observed [design judgment: field set and values].

CI: the guarantee. A review the ai-review job itself produced (source ci), in a runner other than the implementer's
session, tied to the PR head sha and the run (run_id, run_attempt, sha) and, when the action returns it, the action's
session_id. executed_by process, attestation ci_job, context isolated (isolated from the implementer session).

Local: advisory only, never a merge condition. The record carries
  tool         claude_code | codex | other   the real tool (flag --tool, AGENTIVE_TOOL, CLAUDECODE), never a constant
  executed_by  subagent | process | self | unknown
  attestation  hook_observed | self_reported
  context      isolated | shared | unverified
  session_id, agent_id, agent_type, reviewed_ref
`isolated` only when a SubagentStart or SubagentStop hook (.agents/hooks/subagent-observe.py ->
.agents/state/subagents.jsonl) saw a subagent of the task's agent type, with an agent_id, inside the task's time window.
The orchestrator says how it ran the task by writing {"executed_by": "subagent" | "self"} to the task's meta path
(`meta=` of the AGENTIVE_REVIEW_TASK line); `self` (the do-it-yourself fallback) is recorded as context shared. No
meta and no hook line: executed_by unknown, context unverified. Codex local reviews have no hook observation.
"""
import datetime
import json
import os

from .. import common

TOOLS = ("claude_code", "codex", "other")
EXECUTED_BY = ("subagent", "process", "self", "unknown")
ATTESTATION = ("hook_observed", "self_reported", "ci_job")
CONTEXT = ("isolated", "shared", "unverified")
SLACK_S = 10


def detect_tool(flag=None):
    """The real tool: --tool, then AGENTIVE_TOOL, then CLAUDECODE; else other (never guessed as claude_code)."""
    for v in (flag, os.environ.get("AGENTIVE_TOOL")):
        if v in TOOLS:
            return v
    return "claude_code" if os.environ.get("CLAUDECODE") else "other"


def ci_fields():
    return {"executed_by": "process", "attestation": "ci_job", "context": "isolated"}


def _ts(s):
    try:
        return datetime.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def read_observations(repo):
    out = []
    try:
        with open(os.path.join(repo, ".agents", "state", "subagents.jsonl"), encoding="utf-8") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("event") in ("start", "stop") and _ts(e.get("ts")) is not None:
                    out.append(e)
    except OSError:
        pass
    return out


def observe(repo, agent_type, since, until):
    """The hook line of a subagent of this type with an agent_id inside [since - slack, until + slack] (epoch seconds):
    {agent_id, agent_type, session_id} or None. A stop line wins over a start line (it proves the subagent finished)."""
    best = None
    for e in read_observations(repo):
        if e.get("agent_type") != agent_type or not e.get("agent_id"):
            continue
        t = _ts(e["ts"])
        if t < since - SLACK_S or t > until + SLACK_S:
            continue
        if best is None or (e["event"] == "stop", t) >= (best["event"] == "stop", _ts(best["ts"])):
            best = e
    return {k: best.get(k) for k in ("agent_id", "agent_type", "session_id")} if best else None


def local_fields(repo, tool, agent, in_path, out_path, meta, reviewed_ref=None):
    """The honest local record of one task (dict with the schema fields; empty values are left out)."""
    claimed = (meta or {}).get("executed_by")
    claimed = claimed if claimed in ("subagent", "self") else None
    obs = None
    if tool == "claude_code" and claimed != "self":
        try:
            since, until = os.path.getmtime(in_path), os.path.getmtime(out_path)
        except OSError:
            since = until = None
        if since is not None:
            obs = observe(repo, agent, since, until)
    if obs:
        rec = {"executed_by": "subagent", "attestation": "hook_observed", "context": "isolated", "agent_id": obs["agent_id"],
               "agent_type": obs["agent_type"]}
        if obs.get("session_id"):
            rec["session_id"] = obs["session_id"]
    elif claimed == "self":
        rec = {"executed_by": "self", "attestation": "self_reported", "context": "shared"}
    elif claimed == "subagent":
        rec = {"executed_by": "subagent", "attestation": "self_reported", "context": "unverified"}
    else:
        rec = {"executed_by": "unknown", "attestation": "self_reported", "context": "unverified"}
    rec["tool"] = tool
    if reviewed_ref:
        rec["reviewed_ref"] = reviewed_ref
    return rec


def read_meta(path):
    return common.read_json(path, {}) if os.path.exists(path) else {}
